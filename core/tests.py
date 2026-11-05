from datetime import date, datetime, time, timedelta
from django.test import TestCase
from django.urls import reverse
from decimal import Decimal
from django.utils import timezone

from .models import (
    AdvanceRequest, CustomUser, DeletedStudent, FaceIDLog, Group, Manager, MonthlyPayment,
    StaffAbsenceExemption, Student, Teacher, TeacherSchedule, TimeSlot,
)
from core.face_service import process_face_check_in


class ManagerDashboardAndPermissionsTests(TestCase):
    def setUp(self):
        self.manager_user = CustomUser.objects.create_user(
            username='m_test_101',
            password='ManagerPass123',
            role='manager',
            first_name='Sardor',
            last_name='Menejerov',
        )
        self.manager = Manager.objects.create(
            user=self.manager_user,
            first_name='Sardor',
            last_name='Menejerov',
            phone='+998901234567',
            generated_login='m_test_101',
            generated_password='ManagerPass123',
            monthly_salary=4000000,
        )

        self.teacher_user = CustomUser.objects.create_user(
            username='t_test_202',
            password='TeacherPass123',
            role='teacher',
            first_name='Malika',
            last_name='Oqituvchi',
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Malika',
            last_name='Oqituvchi',
            generated_login='t_test_202',
            generated_password='TeacherPass123',
        )

        self.group = Group.objects.create(
            name='Ingliz tili A1',
            subject='Ingliz tili',
            teacher=self.teacher,
        )
        self.timeslot = TimeSlot.objects.create(
            group=self.group,
            days='Dushanba-Chorshanba-Juma',
            start_time='09:00',
            end_time='11:00',
        )
        self.student = Student.objects.create(
            first_name='Ali',
            last_name='Valiyev',
            phone_1='+998901112233',
            time_slot=self.group.time_slots.first(),
            payment_rate=500000,
        )

    def test_manager_login_clears_stale_admin_session_and_redirects_to_manager_dashboard(self):
        session = self.client.session
        session['admin_authenticated'] = True
        session.save()

        response = self.client.post(reverse('manager_login'), {
            'username': 'm_test_101',
            'password': 'ManagerPass123',
        })
        self.assertRedirects(response, reverse('manager_dashboard'))
        self.assertFalse(self.client.session.get('admin_authenticated', False))

    def test_manager_login_page_accessible_when_logged_in_as_other_role(self):
        parent_user = CustomUser.objects.create_user(
            username='p_test',
            password='ParentPass123',
            role='parent',
        )
        self.client.force_login(parent_user)
        response = self.client.get(reverse('manager_login'))
        self.assertEqual(response.status_code, 200)

    def test_manager_dashboard_and_all_linked_buttons_return_200(self):
        self.client.force_login(self.manager_user)

        urls_to_check = [
            reverse('manager_dashboard'),
            reverse('admin_dashboard'),
            reverse('admin_payments_all'),
            reverse('admin_group_payments', args=[self.group.id]),
            reverse('admin_group_create'),
            reverse('admin_group_detail', args=[self.group.id]),
            reverse('admin_student_create', args=[self.timeslot.id]),
            reverse('admin_student_edit', args=[self.student.id]),
            reverse('admin_temp_students'),
            reverse('admin_deleted_students'),
            reverse('admin_advance_list'),
            reverse('staff_my_face_logs'),
            reverse('face_id_check_in'),
        ]

        for url in urls_to_check:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(
                    resp.status_code,
                    200,
                    f"Expected 200 OK for manager on {url}, got {resp.status_code} (redirect to {getattr(resp, 'url', None)})",
                )

    def test_manager_can_save_and_delete_student_payment(self):
        self.client.force_login(self.manager_user)

        # Check Save button is rendered for Manager on payments page
        page_resp = self.client.get(reverse('admin_payments_all'))
        self.assertContains(page_resp, 'Saqlash')

        # Save payment via AJAX
        save_resp = self.client.post(
            reverse('admin_payment_save'),
            {
                'student_id': self.student.id,
                'month': '2026-11',
                'amount': '500000',
                'payment_type': 'cash',
                'ajax': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(save_resp.status_code, 200)
        data = save_resp.json()
        self.assertEqual(data['status'], 'success')
        payment_id = data['payment_id']
        self.assertTrue(MonthlyPayment.objects.filter(id=payment_id).exists())

        # Delete payment via GET link (as used in template)
        del_resp = self.client.get(reverse('admin_payment_delete', args=[payment_id]))
        self.assertEqual(del_resp.status_code, 302)
        self.assertFalse(MonthlyPayment.objects.filter(id=payment_id).exists())

    def test_manager_can_request_and_review_advance(self):
        self.client.force_login(self.manager_user)

        req_resp = self.client.post(
            reverse('staff_advance_request'),
            {
                'amount': '250000',
                'month': '2026-11',
                'reason': 'Shaxsiy ehtiyoj',
            },
            HTTP_REFERER=reverse('manager_dashboard'),
        )
        self.assertEqual(req_resp.status_code, 302)
        adv = AdvanceRequest.objects.filter(user=self.manager_user).first()
        self.assertIsNotNone(adv)
        self.assertEqual(adv.status, 'pending')

        approve_resp = self.client.post(
            reverse('admin_advance_action', args=[adv.id]),
            {
                'action': 'approve',
                'admin_comment': 'Tasdiqlandi',
            },
        )
        self.assertRedirects(approve_resp, reverse('admin_advance_list'))
        adv.refresh_from_db()
        self.assertEqual(adv.status, 'approved')


class FaceIDCheckInCheckOutTests(TestCase):
    """Face ID API: action='checkin' -> check_in_time, action='checkout' -> check_out_time."""

    def setUp(self):
        from .models import FaceIDLog  # noqa: F401
        self.user = CustomUser.objects.create_user(username='m_face_1', password='Pass12345', role='manager')
        self.manager = Manager.objects.create(
            user=self.user, first_name='Face', last_name='Test',
            generated_login='m_face_1', generated_password='Pass12345', monthly_salary=3000000,
        )
        self.url = reverse('face_id_verify_api')
        self.descriptor = [0.01] * 128

    def _post(self, action=None):
        import json
        from unittest.mock import patch
        payload = {'descriptor': self.descriptor, 'is_kiosk': '1'}
        if action:
            payload['action'] = action
        with patch('core.face_id_views.match_staff_by_face', return_value=(None, self.manager, 97.0, None)):
            resp = self.client.post(self.url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _today_log(self):
        from django.utils import timezone
        from .models import FaceIDLog
        return FaceIDLog.objects.filter(user=self.user, date=timezone.localdate()).first()

    def test_checkout_without_checkin_is_rejected(self):
        data = self._post('checkout')
        self.assertFalse(data['success'])
        self.assertEqual(data['error_type'], 'no_check_in')
        self.assertIsNone(self._today_log())

    def test_checkin_then_checkout_flow(self):
        data = self._post('checkin')
        self.assertTrue(data['success'])
        self.assertEqual(data['action_type'], 'kelish')
        log = self._today_log()
        self.assertIsNotNone(log.check_in_time)
        self.assertIsNone(log.check_out_time)

        # "Keldim" rejimida qayta skan — ketish deb yozilmasligi kerak
        data = self._post('checkin')
        self.assertTrue(data['success'])
        self.assertEqual(data['action_type'], 'already_checked_in')
        self.assertIsNone(self._today_log().check_out_time)

        # "Ketdim" — check_out_time yoziladi
        data = self._post('checkout')
        self.assertTrue(data['success'])
        self.assertEqual(data['action_type'], 'ketish')
        self.assertIsNotNone(data['check_out_time'])
        log = self._today_log()
        self.assertIsNotNone(log.check_out_time)

        # Darhol takroriy "Ketdim" — dublikat sifatida qaytadi
        data = self._post('checkout')
        self.assertEqual(data['action_type'], 'already_checked_out')

    def test_legacy_mode_without_action_still_records_check_in(self):
        data = self._post()
        self.assertTrue(data['success'])
        self.assertEqual(data['action_type'], 'kelish')
        self.assertIsNotNone(self._today_log().check_in_time)


class TeacherStudentDeletePermissionsTests(TestCase):
    """O'qituvchilar paneli va backend'da o'quvchini o'chirish taqiqlanganligini tekshirish."""

    def setUp(self):
        self.teacher_user = CustomUser.objects.create_user(
            username='t_math_teacher',
            password='TeacherPass123',
            role='teacher',
            first_name='Zuhra',
            last_name='Karimova',
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Zuhra',
            last_name='Karimova',
            generated_login='t_math_teacher',
            generated_password='TeacherPass123',
        )
        self.manager_user = CustomUser.objects.create_user(
            username='m_office_manager',
            password='ManagerPass123',
            role='manager',
            first_name='Jasur',
            last_name='Rahimov',
        )
        self.manager = Manager.objects.create(
            user=self.manager_user,
            first_name='Jasur',
            last_name='Rahimov',
            generated_login='m_office_manager',
            generated_password='ManagerPass123',
        )
        self.group = Group.objects.create(
            name='Matematika 5-sinf',
            subject='Matematika',
            teacher=self.teacher,
        )
        self.timeslot = TimeSlot.objects.create(
            group=self.group,
            days='Seshanba-Payshanba-Shanba',
            start_time='14:00',
            end_time='16:00',
        )
        self.student = Student.objects.create(
            first_name='Davron',
            last_name='Sobirov',
            phone_1='+998909876543',
            time_slot=self.timeslot,
        )

    def test_teacher_dashboard_does_not_contain_delete_button_or_modal(self):
        self.client.force_login(self.teacher_user)
        resp = self.client.get(reverse('teacher_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'btn-trigger-teacher-delete')
        self.assertNotContains(resp, 'teacherDeleteModal')
        self.assertNotContains(resp, "O'quvchini guruhdan chiqarish")

    def test_teacher_cannot_delete_student_via_url(self):
        self.client.force_login(self.teacher_user)
        resp = self.client.post(reverse('teacher_student_delete', args=[self.student.id]), {
            'reason': 'Darsga qatnashmadi',
        })
        self.assertEqual(resp.status_code, 403)
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        self.assertIsNotNone(self.student.time_slot)

    def test_teacher_cannot_use_admin_delete_student(self):
        self.client.force_login(self.teacher_user)
        resp = self.client.post(reverse('admin_student_delete', args=[self.student.id]), {
            'reason': 'Admin orqali ochirishga urinish',
        })
        # @admin_required dekoratori o'qituvchini teacher_dashboard ga qaytaradi (302)
        self.assertIn(resp.status_code, (302, 403))
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)

    def test_manager_can_delete_student(self):
        self.client.force_login(self.manager_user)
        resp = self.client.post(reverse('admin_student_delete', args=[self.student.id]), {
            'reason': 'Manager tomonidan arxivlandi',
        })
        self.assertEqual(resp.status_code, 302)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)
        self.assertIsNone(self.student.time_slot)


class StaffAbsenceExemptionTests(TestCase):
    def setUp(self):
        self.admin_user = CustomUser.objects.create_superuser(
            username='admin_boss',
            password='AdminPass123',
            role='admin',
            first_name='Admin',
            last_name='Boshliq',
        )
        self.manager_user = CustomUser.objects.create_user(
            username='manager_alisher',
            password='ManagerPass123',
            role='manager',
            first_name='Alisher',
            last_name='Menejerov',
        )
        self.manager = Manager.objects.create(
            user=self.manager_user,
            first_name='Alisher',
            last_name='Menejerov',
            generated_login='manager_alisher',
            generated_password='ManagerPass123',
            monthly_salary=5000000,
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='teacher_dilnoza',
            password='TeacherPass123',
            role='teacher',
            first_name='Dilnoza',
            last_name='Ustozova',
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Dilnoza',
            last_name='Ustozova',
            generated_login='teacher_dilnoza',
            generated_password='TeacherPass123',
            monthly_salary=3000000,
            late_fine_rate=2.0,
        )
        self.today = date.today()

    def test_absence_exemption_mandatory_reason_validation(self):
        """Kelmaslik sababi majburiy kiritilishi shart (bo'sh yoki 3 belgidan kam bo'lsa rad etiladi)."""
        self.client.force_login(self.manager_user)

        # 1. Bo'sh sabab bilan yuborish
        resp1 = self.client.post(reverse('admin_absence_exemption_create'), {
            'user_id': self.teacher_user.id,
            'date': self.today.strftime('%Y-%m-%d'),
            'reason': '   ',
        })
        self.assertEqual(resp1.status_code, 302)
        self.assertFalse(StaffAbsenceExemption.objects.filter(user=self.teacher_user, date=self.today).exists())

        # 2. Juda qisqa sabab (<3 belgi) bilan yuborish
        resp2 = self.client.post(reverse('admin_absence_exemption_create'), {
            'user_id': self.teacher_user.id,
            'date': self.today.strftime('%Y-%m-%d'),
            'reason': 'ok',
        })
        self.assertEqual(resp2.status_code, 302)
        self.assertFalse(StaffAbsenceExemption.objects.filter(user=self.teacher_user, date=self.today).exists())

    def test_absence_exemption_creation_success(self):
        """Menejer yoki Admin sabab kiritganda istisno saqlanadi va FaceIDLog excused holatga o'tadi."""
        self.client.force_login(self.manager_user)
        reason_text = "Kasallik varaqasi asosida ruxsat berildi"

        resp = self.client.post(reverse('admin_absence_exemption_create'), {
            'user_id': self.teacher_user.id,
            'date': self.today.strftime('%Y-%m-%d'),
            'reason': reason_text,
        })
        self.assertEqual(resp.status_code, 302)

        # StaffAbsenceExemption tekshirish
        exemption = StaffAbsenceExemption.objects.filter(user=self.teacher_user, date=self.today).first()
        self.assertIsNotNone(exemption)
        self.assertEqual(exemption.reason, reason_text)
        self.assertEqual(exemption.created_by, self.manager_user)

        # FaceIDLog holati tekshirish (jarima 0 bo'lishi shart)
        face_log = FaceIDLog.objects.filter(user=self.teacher_user, date=self.today).first()
        self.assertIsNotNone(face_log)
        self.assertEqual(face_log.status, 'excused')
        self.assertFalse(face_log.is_late)
        self.assertEqual(face_log.late_minutes, 0)
        self.assertEqual(face_log.fine_percent_applied, 0)
        self.assertEqual(face_log.fine_amount_deducted, 0)

    def test_face_check_in_with_active_exemption_protects_staff(self):
        """Istisno qilingan kuni Face ID skaner qilinsa ham xodimga jarima yozilmaydi, excused qaytadi."""
        StaffAbsenceExemption.objects.create(
            user=self.teacher_user,
            date=self.today,
            reason="Xizmat safari (Seminar)",
            created_by=self.admin_user,
        )

        from datetime import time
        # Belgilangan ish vaqti 09:00, xodim 11:30 da kech qolib skan qilsa ham:
        result = process_face_check_in(self.teacher_user, self.today, time(11, 30))

        self.assertTrue(result['success'])
        self.assertTrue(result.get('is_excused'))
        self.assertEqual(result.get('exemption_reason'), "Xizmat safari (Seminar)")
        self.assertFalse(result.get('is_late', False))
        self.assertEqual(result.get('fine_amount', 0), 0)

        log = FaceIDLog.objects.filter(user=self.teacher_user, date=self.today).first()
        self.assertEqual(log.status, 'excused')
        self.assertFalse(log.is_late)
        self.assertEqual(log.fine_amount_deducted, 0)

    def test_delete_absence_exemption(self):
        """Istisnoni bekor qilish muvaffaqiyatli ishlaydi."""
        exemption = StaffAbsenceExemption.objects.create(
            user=self.teacher_user,
            date=self.today,
            reason="Vaqtincha ruxsat",
            created_by=self.manager_user,
        )
        self.client.force_login(self.manager_user)

        resp = self.client.post(reverse('admin_absence_exemption_delete', args=[exemption.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(StaffAbsenceExemption.objects.filter(id=exemption.id).exists())

    def test_teacher_cannot_create_exemption(self):
        """O'qituvchi o'ziga yoki boshqalarga istisno yarata olmaydi."""
        self.client.force_login(self.teacher_user)
        resp = self.client.post(reverse('admin_absence_exemption_create'), {
            'user_id': self.teacher_user.id,
            'date': self.today.strftime('%Y-%m-%d'),
            'reason': "O'zimga o'zim ruxsat berdim",
        })
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(StaffAbsenceExemption.objects.filter(user=self.teacher_user, date=self.today).exists())


class StudentJoinedDateScopingTests(TestCase):
    def setUp(self):
        self.manager_user = CustomUser.objects.create_user(
            username='mgr_test',
            password='ManagerPass123',
            role='manager',
            first_name='Menejer',
            last_name='Sinovchi',
        )
        self.manager = Manager.objects.create(
            user=self.manager_user,
            first_name='Menejer',
            last_name='Sinovchi',
            phone='+998901234500',
            generated_login='mgr_test',
            generated_password='ManagerPass123',
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='tch_test',
            password='TeacherPass123',
            role='teacher',
            first_name='Ustoz',
            last_name='Sinovchi',
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Ustoz',
            last_name='Sinovchi',
            generated_login='tch_test',
            generated_password='TeacherPass123',
        )
        self.group = Group.objects.create(
            name='Matematika 1',
            subject='Matematika',
            teacher=self.teacher,
        )
        self.timeslot = TimeSlot.objects.create(
            group=self.group,
            days='Du-Chor-Ju',
            start_time='14:00',
            end_time='16:00',
        )
        # 2026-11-01 da qo'shilgan yangi o'quvchi
        self.new_student = Student.objects.create(
            first_name='Yangi',
            last_name='Oquvchi',
            phone_1='+998909998877',
            time_slot=self.timeslot,
            payment_rate=400000,
            joined_date=date(2026, 11, 1),
        )
        # 2026-09-01 da qo'shilgan eski o'quvchi
        self.old_student = Student.objects.create(
            first_name='Eski',
            last_name='Oquvchi',
            phone_1='+998901110000',
            time_slot=self.timeslot,
            payment_rate=300000,
            joined_date=date(2026, 9, 1),
        )

    def test_new_student_not_visible_in_past_month_payments(self):
        """2026-11 da qo'shilgan o'quvchi 2026-10 (o'tgan oy) to'lovlar jadvaliga kirmasligi lozim."""
        from core.payment_views import _build_payments_data, get_active_students_qs, filter_students_by_payment_status

        qs = get_active_students_qs(group=self.group)
        rows, stats = _build_payments_data(qs, req_month='2026-10')
        student_ids_in_rows = [r['student_id'] for r in rows]

        self.assertIn(self.old_student.id, student_ids_in_rows)
        self.assertNotIn(self.new_student.id, student_ids_in_rows)
        # Statistika faqat eski o'quvchi bo'yicha hisoblanadi (300 000, 400 000 qo'shilmasligi kerak)
        self.assertEqual(stats['total_rate'], 300000)
        self.assertEqual(stats['total_students'], 1)

        # filter_students_by_payment_status ham o'tgan oyni to'g'ri filtrlashi kerak
        unpaid_qs = filter_students_by_payment_status(qs, req_month='2026-10', status_filter='unpaid')
        unpaid_ids = list(unpaid_qs.values_list('id', flat=True))
        self.assertIn(self.old_student.id, unpaid_ids)
        self.assertNotIn(self.new_student.id, unpaid_ids)

    def test_new_student_visible_in_current_and_future_month_payments(self):
        """2026-11 da qo'shilgan o'quvchi 2026-11 (joriy oy) to'lovlar jadvalida ko'rinishi lozim."""
        from core.payment_views import _build_payments_data, get_active_students_qs

        qs = get_active_students_qs(group=self.group)
        rows, stats = _build_payments_data(qs, req_month='2026-11')
        student_ids_in_rows = [r['student_id'] for r in rows]

        self.assertIn(self.old_student.id, student_ids_in_rows)
        self.assertIn(self.new_student.id, student_ids_in_rows)
        self.assertEqual(stats['total_rate'], 700000)
        self.assertEqual(stats['total_students'], 2)

    def test_new_student_not_visible_in_past_month_attendance(self):
        """2026-11 da qo'shilgan o'quvchi 2026-10 davomat jurnali (_student_visible_in_month) da ko'rinmasligi lozim."""
        from core.teacher_views import _student_visible_in_month

        self.assertFalse(_student_visible_in_month(self.new_student, year=2026, month=10))
        self.assertTrue(_student_visible_in_month(self.new_student, year=2026, month=11))
        self.assertTrue(_student_visible_in_month(self.old_student, year=2026, month=10))

    def test_prevent_payment_creation_for_past_months_before_student_joined(self):
        """O'quvchi qo'shilishidan oldingi oylar uchun to'lov kiritishga yo'l qo'yilmaydi."""
        self.client.force_login(self.manager_user)

        resp = self.client.post(
            reverse('admin_payment_save'),
            {
                'student_id': self.new_student.id,
                'month': '2026-10',  # O'quvchi 2026-11 da kelgan
                'amount': '400000',
                'payment_type': 'cash',
                'ajax': '1',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data['status'], 'error')
        self.assertIn("oldingi oy", data['msg'])
        self.assertFalse(MonthlyPayment.objects.filter(student=self.new_student, month='2026-10').exists())





class GroupArchiveAndStudentIsolationTests(TestCase):
    def setUp(self):
        self.manager_user = CustomUser.objects.create_user(
            username='mgr_arch',
            password='ManagerPass123',
            role='manager',
            first_name='Menejer',
            last_name='Arxiv',
        )
        self.manager = Manager.objects.create(
            user=self.manager_user,
            first_name='Menejer',
            last_name='Arxiv',
            phone='+998901112299',
            generated_login='mgr_arch',
            generated_password='ManagerPass123',
        )

        self.teacher_user = CustomUser.objects.create_user(
            username='tch_arch',
            password='TeacherPass123',
            role='teacher',
            first_name='Zarif',
            last_name='Ustoz',
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Zarif',
            last_name='Ustoz',
            generated_login='tch_arch',
            generated_password='TeacherPass123',
        )

        self.group1 = Group.objects.create(
            name='Fizika Guruh A',
            subject='Fizika',
            teacher=self.teacher,
        )
        self.group2 = Group.objects.create(
            name='Kimyo Guruh B',
            subject='Kimyo',
            teacher=self.teacher,
        )

        self.slot1 = TimeSlot.objects.create(
            group=self.group1,
            days='Du-Chor-Ju',
            start_time='10:00',
            end_time='12:00',
        )
        self.slot2 = TimeSlot.objects.create(
            group=self.group2,
            days='Se-Pay-Sha',
            start_time='14:00',
            end_time='16:00',
        )

        self.student1 = Student.objects.create(
            first_name='Davron',
            last_name='Kimsanov',
            phone_1='+998901234561',
            time_slot=self.slot1,
            payment_rate=450000,
            joined_date=date(2026, 9, 1),
            is_active=True,
        )
        self.student2 = Student.objects.create(
            first_name='Jasur',
            last_name='Rahimov',
            phone_1='+998901234562',
            time_slot=self.slot2,
            payment_rate=500000,
            joined_date=date(2026, 9, 1),
            is_active=True,
        )

    def test_archive_student_stores_group_and_reason_properly(self):
        """O'quvchini arxivlaganda qaysi guruhdan va nima sababdan o'chirilgani aniq saqlanadi."""
        self.client.force_login(self.manager_user)
        resp = self.client.post(
            reverse('admin_student_delete', args=[self.student1.id]),
            {'reason': 'Boshqa shaharga kochib ketdi'},
        )
        self.assertEqual(resp.status_code, 302)

        # Student holati
        self.student1.refresh_from_db()
        self.assertFalse(self.student1.is_active)
        self.assertIsNotNone(self.student1.left_date)
        self.assertIsNone(self.student1.time_slot)

        # DeletedStudent yozuvi
        ds = DeletedStudent.objects.filter(student_id=self.student1.id).first()
        self.assertIsNotNone(ds)
        self.assertEqual(ds.group_id, self.group1.id)
        self.assertEqual(ds.group_name, self.group1.name)
        self.assertEqual(ds.reason, "Boshqa shaharga kochib ketdi")

    def test_archived_student_completely_isolated_from_payments_and_reports(self):
        """Arxivlangan o'quvchi to'lovlar, qarzdorlar va hisobotlarda MUTLAQO ko'rinmaydi."""
        from core.payment_views import get_active_students_qs, _build_payments_data, filter_students_by_payment_status

        # 1-o'quvchini arxivlaymiz
        from core.student_views import _archive_student_record
        _archive_student_record(self.student1, reason="Kursni tugatdi")

        # get_active_students_qs
        active_ids = list(get_active_students_qs().values_list('id', flat=True))
        self.assertNotIn(self.student1.id, active_ids)
        self.assertIn(self.student2.id, active_ids)

        # To'lovlar ro'yxatida ko'rinmaydi
        rows, stats = _build_payments_data(get_active_students_qs(), req_month='2026-11')
        row_student_ids = [r['student_id'] for r in rows]
        self.assertNotIn(self.student1.id, row_student_ids)
        self.assertIn(self.student2.id, row_student_ids)

        # Qarzdorlar ro'yxatiga kirmaydi
        unpaid_qs = filter_students_by_payment_status(get_active_students_qs(), req_month='2026-11', status_filter='unpaid')
        unpaid_ids = list(unpaid_qs.values_list('id', flat=True))
        self.assertNotIn(self.student1.id, unpaid_ids)

    def test_group_archive_pages_for_admin_and_teacher(self):
        """Admin, Menejer va O'qituvchi guruh arxivi sahifasi to'g'ri ishlaydi."""
        from core.student_views import _archive_student_record
        _archive_student_record(self.student1, reason="Vaxti togri kelmadi")
        _archive_student_record(self.student2, reason="Moddiy qiyinchilik")

        # 1. Admin/Menejer guruh arxivi
        self.client.force_login(self.manager_user)
        resp1 = self.client.get(reverse('admin_group_archive', args=[self.group1.id]))
        self.assertEqual(resp1.status_code, 200)
        self.assertContains(resp1, self.student1.first_name)
        self.assertContains(resp1, "Vaxti togri kelmadi")
        # group2 dagi o'quvchi group1 arxivida ko'rinmasligi kerak
        self.assertNotContains(resp1, self.student2.first_name)

        # 2. O'qituvchi guruh arxivi
        self.client.force_login(self.teacher_user)
        t_resp = self.client.get(reverse('teacher_group_archive', args=[self.group1.id]))
        self.assertEqual(t_resp.status_code, 200)
        self.assertContains(t_resp, self.student1.first_name)
        self.assertContains(t_resp, "Vaxti togri kelmadi")
        self.assertNotContains(t_resp, self.student2.first_name)

        # 3. Admin group detail sahifasida inline collapse arxiv mavjudligi
        self.client.force_login(self.manager_user)
        resp_detail = self.client.get(reverse('admin_group_detail', args=[self.group1.id]))
        self.assertEqual(resp_detail.status_code, 200)
        self.assertContains(resp_detail, 'id="groupArchiveCollapse"')
        self.assertContains(resp_detail, self.student1.first_name)
        self.assertContains(resp_detail, "Vaxti togri kelmadi")

        # 4. Teacher dashboardda inline collapse arxiv mavjudligi
        self.client.force_login(self.teacher_user)
        resp_tdash = self.client.get(reverse('teacher_dashboard'))
        self.assertEqual(resp_tdash.status_code, 200)
        self.assertContains(resp_tdash, f'id="teacherGroupArchive{self.group1.id}"')
        self.assertContains(resp_tdash, self.student1.first_name)
        self.assertContains(resp_tdash, "Vaxti togri kelmadi")

    def test_general_archive_filtering_by_group(self):
        """Umumiy arxiv sahifasida guruh bo'yicha filtrlash to'g'ri ishlaydi."""
        from core.student_views import _archive_student_record
        _archive_student_record(self.student1, reason="Sabab 1")
        _archive_student_record(self.student2, reason="Sabab 2")

        self.client.force_login(self.manager_user)
        # Barcha arxiv
        resp_all = self.client.get(reverse('admin_deleted_students'))
        self.assertEqual(resp_all.status_code, 200)
        self.assertContains(resp_all, self.student1.first_name)
        self.assertContains(resp_all, self.student2.first_name)

        # Faqat group1 bo'yicha filter
        resp_g1 = self.client.get(reverse('admin_deleted_students') + f'?group_id={self.group1.id}')
        self.assertEqual(resp_g1.status_code, 200)
        self.assertContains(resp_g1, self.student1.first_name)
        self.assertNotContains(resp_g1, self.student2.first_name)


class TeacherScheduleAndFaceIDTests(TestCase):
    def setUp(self):
        self.admin_user = CustomUser.objects.create_superuser(
            username='admin_boss', password='BossPass123', email='boss@humo.uz', role='admin'
        )
        self.manager_user = CustomUser.objects.create_user(
            username='manager_sardor', password='ManagerPass123', role='manager',
            first_name='Sardor', last_name='Menejerov'
        )
        self.manager = Manager.objects.create(
            user=self.manager_user,
            first_name='Sardor',
            last_name='Menejerov',
            generated_login='manager_sardor',
            generated_password='ManagerPass123',
        )

        self.teacher_user = CustomUser.objects.create_user(
            username='teacher_dilshod', password='TeacherPass123', role='teacher',
            first_name='Dilshod', last_name='Karimov'
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Dilshod',
            last_name='Karimov',
            phone='+998901112233',
            generated_login='teacher_dilshod',
            generated_password='TeacherPass123',
            work_start_time=time(8, 0),
            work_end_time=time(17, 0),
            monthly_salary=Decimal('5000000.00'),
            late_fine_rate=Decimal('2.0'),
        )
        self.today = date.today()
        self.weekday = self.today.weekday()

    def test_teacher_schedule_fallback_and_custom(self):
        """Maxsus jadval bo'lmaganda standart vaqt fallback qiladi, kiritilganda esa maxsus jadval ishlaydi."""
        # Fallback holat
        sched_fallback = self.teacher.get_schedule_for_date(self.today)
        self.assertEqual(sched_fallback['work_start_time'], time(8, 0))
        self.assertEqual(sched_fallback['work_end_time'], time(17, 0))
        self.assertTrue(sched_fallback['is_working_day'])
        self.assertFalse(sched_fallback['is_custom'])

        # Maxsus bugungi kun jadvali kiritilganda
        TeacherSchedule.objects.create(
            teacher=self.teacher,
            weekday=self.weekday,
            work_start_time=time(13, 30),
            work_end_time=time(19, 0),
            is_working_day=True,
            note="Kechki smena"
        )
        sched_custom = self.teacher.get_schedule_for_date(self.today)
        self.assertEqual(sched_custom['work_start_time'], time(13, 30))
        self.assertEqual(sched_custom['work_end_time'], time(19, 0))
        self.assertTrue(sched_custom['is_custom'])
        self.assertEqual(sched_custom['note'], "Kechki smena")

    def test_face_id_check_in_uses_weekday_schedule_instead_of_default(self):
        """Standart vaqt 08:00 bo'lsa-da, bugungi grafik 14:00 bo'lsa, 13:50 da kelgan o'qituvchiga jarima yozilmaydi."""
        TeacherSchedule.objects.create(
            teacher=self.teacher,
            weekday=self.weekday,
            work_start_time=time(14, 0),
            work_end_time=time(20, 0),
            is_working_day=True,
        )

        from unittest.mock import patch
        checkin_dt = timezone.make_aware(datetime.combine(self.today, time(13, 50)))

        with patch('django.utils.timezone.localtime', return_value=checkin_dt):
            res = process_face_check_in(
                user=self.teacher_user,
                action='checkin',
                bypass_geo=True,
                pre_verified=True,
            )

        self.assertTrue(res['success'])
        self.assertFalse(res['is_late'])
        self.assertEqual(res['late_minutes'], 0)
        self.assertEqual(res['fine_amount'], 0.0)

        log = FaceIDLog.objects.filter(user=self.teacher_user, date=self.today).first()
        self.assertIsNotNone(log)
        self.assertFalse(log.is_late)
        self.assertEqual(log.late_minutes, 0)
        self.assertEqual(log.status, 'on_time')

    def test_face_id_check_in_within_30_minutes_grace_period_no_penalty(self):
        """O'qituvchi 30 daqiqagacha kechikib kelsa (Grace Period), unga hech qanday jarima hisoblanmaydi (0 jarima)."""
        TeacherSchedule.objects.create(
            teacher=self.teacher,
            weekday=self.weekday,
            work_start_time=time(11, 0),
            work_end_time=time(18, 0),
            is_working_day=True,
        )

        from unittest.mock import patch
        # 11:00 darsga 11:25 da keldi (25 daqiqa kechikish <= 30 daqiqa imtiyoz)
        checkin_dt = timezone.make_aware(datetime.combine(self.today, time(11, 25)))

        with patch('django.utils.timezone.localtime', return_value=checkin_dt):
            res = process_face_check_in(
                user=self.teacher_user,
                action='checkin',
                bypass_geo=True,
                pre_verified=True,
            )

        self.assertTrue(res['success'])
        self.assertFalse(res['is_late'])
        self.assertEqual(res['fine_percent'], 0.0)
        self.assertEqual(res['fine_amount'], 0.0)
        self.assertEqual(res['late_minutes'], 25)

        log = FaceIDLog.objects.filter(user=self.teacher_user, date=self.today).first()
        self.assertFalse(log.is_late)
        self.assertEqual(log.late_minutes, 25)
        self.assertEqual(log.fine_percent_applied, Decimal('0.00'))
        self.assertEqual(log.fine_amount_deducted, Decimal('0.00'))
        self.assertEqual(log.status, 'on_time')
        self.assertIn("imtiyoz doirasida", log.notes)

    def test_face_id_check_in_over_30_minutes_penalty_calculation(self):
        """30 daqiqadan oshgan har bir minut uchun oylikdan 1% jarima ushlab qolinadi (masalan 45 daqiqa = 15%)."""
        from core.face_service import calculate_teacher_penalty
        TeacherSchedule.objects.create(
            teacher=self.teacher,
            weekday=self.weekday,
            work_start_time=time(11, 0),
            work_end_time=time(18, 0),
            is_working_day=True,
        )

        from unittest.mock import patch
        # 11:00 darsga 11:45 da keldi (45 daqiqa kechikish: 45 - 30 = 15 daqiqa x 1% = 15% jarima)
        checkin_dt = timezone.make_aware(datetime.combine(self.today, time(11, 45)))

        with patch('django.utils.timezone.localtime', return_value=checkin_dt):
            res = process_face_check_in(
                user=self.teacher_user,
                action='checkin',
                bypass_geo=True,
                pre_verified=True,
            )

        self.assertTrue(res['success'])
        self.assertTrue(res['is_late'])
        self.assertEqual(res['late_minutes'], 45)
        # 15% jarima: 5,000,000 * 15% = 750,000 so'm
        self.assertEqual(res['fine_percent'], 15.0)
        self.assertEqual(res['fine_amount'], 750000.0)

        log = FaceIDLog.objects.filter(user=self.teacher_user, date=self.today).first()
        self.assertTrue(log.is_late)
        self.assertEqual(log.late_minutes, 45)
        self.assertEqual(log.fine_percent_applied, Decimal('15.00'))
        self.assertEqual(log.fine_amount_deducted, Decimal('750000.00'))
        self.assertEqual(log.status, 'late')

        # Helper funksiyani ham bevosita tekshirish:
        pct, amt, applied = calculate_teacher_penalty(Decimal('5000000'), 30)
        self.assertEqual(pct, Decimal('0.00'))
        self.assertEqual(amt, Decimal('0.00'))
        self.assertFalse(applied)

        pct35, amt35, applied35 = calculate_teacher_penalty(Decimal('5000000'), 35)
        self.assertEqual(pct35, Decimal('5.00'))
        self.assertEqual(amt35, Decimal('250000.00'))
        self.assertTrue(applied35)

    def test_face_id_check_out_early_leave_detection_with_schedule(self):
        """Ketdim bosganda o'sha kungi ketish grafigidan erta ketayotgan bo'lsa ogohlantiriladi."""
        TeacherSchedule.objects.create(
            teacher=self.teacher,
            weekday=self.weekday,
            work_start_time=time(9, 0),
            work_end_time=time(16, 0),
            is_working_day=True,
        )

        from unittest.mock import patch
        # 1. Avval kelishni qayd qilamiz (09:00)
        in_dt = timezone.make_aware(datetime.combine(self.today, time(9, 0)))
        with patch('django.utils.timezone.localtime', return_value=in_dt):
            process_face_check_in(user=self.teacher_user, action='checkin', bypass_geo=True)

        # 2. Ketish vaqti 16:00, lekin u 15:30 da Face ID "Ketdim" bosadi (30 daqiqa erta)
        out_dt = timezone.make_aware(datetime.combine(self.today, time(15, 30)))
        with patch('django.utils.timezone.localtime', return_value=out_dt):
            res_out = process_face_check_in(user=self.teacher_user, action='checkout', bypass_geo=True)

        self.assertTrue(res_out['success'])
        self.assertTrue(res_out.get('is_early_checkout'))
        self.assertEqual(res_out.get('early_minutes'), 30)

        log = FaceIDLog.objects.filter(user=self.teacher_user, date=self.today).first()
        self.assertIsNotNone(log.check_out_time)
        self.assertIn("Erta ketish: 30 daq", log.notes)

    def test_manager_and_admin_can_save_teacher_weekly_schedule(self):
        """Menejer va Admin ikkalasi ham o'qituvchining haftalik grafigini osonlik bilan saqlay oladi."""
        self.client.force_login(self.manager_user)

        # 1. API orqali haftalik grafikni olish
        api_resp = self.client.get(reverse('admin_teacher_schedule_api', args=[self.teacher.id]))
        self.assertEqual(api_resp.status_code, 200)
        api_data = api_resp.json()
        self.assertTrue(api_data['success'])
        self.assertEqual(len(api_data['weekly_schedule']), 7)

        # 2. Yangi grafikni AJAX orqali saqlash (Menejer tomonidan)
        save_payload = {
            'schedules': [
                {'weekday': 0, 'is_working_day': True, 'work_start_time': '10:00', 'work_end_time': '17:00', 'note': 'Du darsi'},
                {'weekday': 1, 'is_working_day': False, 'work_start_time': '09:00', 'work_end_time': '18:00', 'note': 'Dam olish'},
                {'weekday': 2, 'is_working_day': True, 'work_start_time': '10:00', 'work_end_time': '17:00', 'note': 'Chor darsi'},
                {'weekday': 3, 'is_working_day': False, 'work_start_time': '09:00', 'work_end_time': '18:00', 'note': ''},
                {'weekday': 4, 'is_working_day': True, 'work_start_time': '10:00', 'work_end_time': '17:00', 'note': 'Ju darsi'},
                {'weekday': 5, 'is_working_day': False, 'work_start_time': '09:00', 'work_end_time': '18:00', 'note': ''},
                {'weekday': 6, 'is_working_day': False, 'work_start_time': '09:00', 'work_end_time': '18:00', 'note': ''},
            ]
        }
        import json
        save_resp = self.client.post(
            reverse('admin_teacher_schedule_save', args=[self.teacher.id]),
            data=json.dumps(save_payload),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(save_resp.status_code, 200)
        save_data = save_resp.json()
        self.assertTrue(save_data['success'])

        # Bazada tekshirish: Dushanba va Chorshanba kunlari yangilangan
        mon_sched = TeacherSchedule.objects.get(teacher=self.teacher, weekday=0)
        self.assertTrue(mon_sched.is_working_day)
        self.assertEqual(mon_sched.work_start_time, time(10, 0))
        self.assertEqual(mon_sched.note, 'Du darsi')

        tue_sched = TeacherSchedule.objects.get(teacher=self.teacher, weekday=1)
        self.assertFalse(tue_sched.is_working_day)


class FacePhotoValidationTests(TestCase):
    """Face ID uchun rasm formati, tiniqligi (sharpness/blur) va to'g'ri qarash (yaw, pitch, roll) validatsiyasi testlari."""

    def _create_sharp_image_bytes(self, fmt='JPEG', size=(320, 320)):
        """Tiniq (yuqori kontrastli qirralarga ega) test rasmini yaratadi."""
        from PIL import Image, ImageDraw
        import io
        img = Image.new('RGB', size, color=(140, 140, 140))
        draw = ImageDraw.Draw(img)
        # Shaxmat doskasi va yuz silueti chizish (yuqori Laplacian variance hosil qiladi)
        for y in range(20, size[1] - 20, 16):
            for x in range(20, size[0] - 20, 16):
                if ((x // 16) + (y // 16)) % 2 == 0:
                    draw.rectangle([x, y, x + 15, y + 15], fill=(220, 220, 220))
                else:
                    draw.rectangle([x, y, x + 15, y + 15], fill=(50, 50, 50))
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        buf.seek(0)
        return buf

    def _create_blurry_image_bytes(self, fmt='JPEG', size=(320, 320)):
        """Xira (bir xil rangli / detalsiz) test rasmini yaratadi."""
        from PIL import Image
        import io
        img = Image.new('RGB', size, color=(128, 128, 128))
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        buf.seek(0)
        return buf

    def _generate_68_landmarks(self, yaw_shift=0.0, pitch_shift=0.0, roll_deg=0.0):
        """
        68-nuqtali standart frontal yuz nuqtalarini yaratadi.
        yaw_shift: burun uchini chap/o'ngga siljitish (yaw burchagini o'zgartirish)
        pitch_shift: burun uchini tepa/pastga siljitish (pitch burchagini o'zgartirish)
        roll_deg: ko'zlarni qiyshaytirish (roll burchagini o'zgartirish)
        """
        import math
        pts = [{'x': 160.0, 'y': 160.0} for _ in range(68)]
        # Jawline: 0 (chap jag') .. 8 (iyak) .. 16 (o'ng jag')
        pts[0] = {'x': 100.0, 'y': 160.0}
        pts[8] = {'x': 160.0, 'y': 230.0}
        pts[16] = {'x': 220.0, 'y': 160.0}

        # Ko'zlar markazi: chap ko'z (36..41) ~ (130, 140), o'ng ko'z (42..47) ~ (190, 140)
        rad = math.radians(roll_deg)
        dy_roll = 30.0 * math.sin(rad)
        for i in range(36, 42):
            pts[i] = {'x': 130.0, 'y': 140.0 - dy_roll}
        for i in range(42, 48):
            pts[i] = {'x': 190.0, 'y': 140.0 + dy_roll}

        # Burun uchi (30)
        pts[30] = {'x': 160.0 + yaw_shift, 'y': 170.0 + pitch_shift}

        # Og'iz burchaklari (48..54)
        for i in range(48, 55):
            pts[i] = {'x': 160.0, 'y': 200.0}
        return pts

    def test_invalid_format_rejected(self):
        """Ruxsat etilmagan formatdagi (.gif, .bmp) rasmlar rad etilishi kerak."""
        from core.face_service import validate_face_photo
        from django.core.files.uploadedfile import SimpleUploadedFile

        gif_buf = self._create_sharp_image_bytes(fmt='GIF')
        gif_file = SimpleUploadedFile('avatar.gif', gif_buf.read(), content_type='image/gif')
        res = validate_face_photo(gif_file, filename='avatar.gif')
        self.assertFalse(res['valid'])
        self.assertEqual(res['error_type'], 'invalid_format')
        self.assertIn('.jpg', res['message'])

    def test_blurry_photo_rejected(self):
        """Xira (sharpness past) rasm rad etilishi va tushunarli xabar qaytarishi kerak."""
        from core.face_service import validate_face_photo
        from django.core.files.uploadedfile import SimpleUploadedFile

        blur_buf = self._create_blurry_image_bytes(fmt='JPEG')
        blur_file = SimpleUploadedFile('blur.jpg', blur_buf.read(), content_type='image/jpeg')
        frontal_lm = self._generate_68_landmarks()

        res = validate_face_photo(blur_file, filename='blur.jpg', landmarks=frontal_lm)
        self.assertFalse(res['valid'])
        self.assertEqual(res['error_type'], 'blurry_image')
        self.assertIn('xira', res['message'].lower())

    def test_head_pose_yaw_pitch_roll_validation(self):
        """Bosh yon tomonga burilgan (yaw), egilgan (pitch) yoki qiyshaygan (roll) bo'lsa rad etilishi kerak."""
        from core.face_service import validate_face_photo
        from django.core.files.uploadedfile import SimpleUploadedFile

        # 1. Bosh o'ngga burilgan (Yaw > 20°)
        sharp_buf1 = self._create_sharp_image_bytes(fmt='JPEG')
        file1 = SimpleUploadedFile('yaw.jpg', sharp_buf1.read(), content_type='image/jpeg')
        yaw_lm = self._generate_68_landmarks(yaw_shift=25.0)
        res_yaw = validate_face_photo(file1, filename='yaw.jpg', landmarks=yaw_lm)
        self.assertFalse(res_yaw['valid'])
        self.assertEqual(res_yaw['error_type'], 'bad_pose_yaw')
        self.assertIn("kamera obyektiviga", res_yaw['message'])

        # 2. Bosh pastga egilgan (Pitch > 20°)
        sharp_buf2 = self._create_sharp_image_bytes(fmt='JPEG')
        file2 = SimpleUploadedFile('pitch.jpg', sharp_buf2.read(), content_type='image/jpeg')
        pitch_lm = self._generate_68_landmarks(pitch_shift=22.0)
        res_pitch = validate_face_photo(file2, filename='pitch.jpg', landmarks=pitch_lm)
        self.assertFalse(res_pitch['valid'])
        self.assertEqual(res_pitch['error_type'], 'bad_pose_pitch')

        # 3. Bosh yonboshga qiyshaygan (Roll > 18°)
        sharp_buf3 = self._create_sharp_image_bytes(fmt='JPEG')
        file3 = SimpleUploadedFile('roll.jpg', sharp_buf3.read(), content_type='image/jpeg')
        roll_lm = self._generate_68_landmarks(roll_deg=25.0)
        res_roll = validate_face_photo(file3, filename='roll.jpg', landmarks=roll_lm)
        self.assertFalse(res_roll['valid'])
        self.assertEqual(res_roll['error_type'], 'bad_pose_roll')

    def test_valid_sharp_frontal_photo_accepted(self):
        """Format to'g'ri (.jpg/.png), tiniqligi yuqori va kameraga to'g'ri qaragan rasm muvaffaqiyatli o'tadi."""
        from core.face_service import validate_face_photo
        from django.core.files.uploadedfile import SimpleUploadedFile

        sharp_buf = self._create_sharp_image_bytes(fmt='PNG')
        valid_file = SimpleUploadedFile('good.png', sharp_buf.read(), content_type='image/png')
        frontal_lm = self._generate_68_landmarks(yaw_shift=0.0, pitch_shift=0.0, roll_deg=0.0)

        res = validate_face_photo(valid_file, filename='good.png', landmarks=frontal_lm)
        self.assertTrue(res['valid'])
        self.assertIsNone(res['error_type'])
        self.assertGreaterEqual(res['sharpness'], 45.0)
        self.assertAlmostEqual(res['pose']['yaw'], 0.0, places=1)
        self.assertAlmostEqual(res['pose']['roll'], 0.0, places=1)


class FaceMatchingStrictnessAndMultiFaceTests(TestCase):
    """
    Qat'iy threshold (<= 0.45), minimal o'xshashlik foizi (>= 72%),
    ikkilanuvchanlik himoyasi (ambiguity guard) va ko'p yuzlik holatlarni tekshirish testlari.
    """

    def setUp(self):
        self.user1 = CustomUser.objects.create_user(username='t_face_ali', password='Pass12345', role='teacher')
        self.teacher1 = Teacher.objects.create(
            user=self.user1, first_name='Ali', last_name='Valiyev',
            generated_login='t_face_ali', generated_password='Pass12345',
            # Ali uchun standart 128-o'lchamli descriptor
            face_embedding='[0.10]' + ', [0.10]' * 127
        )
        import json
        self.teacher1.face_embedding = json.dumps([0.10] * 128)
        self.teacher1.save()

        self.user2 = CustomUser.objects.create_user(username='t_face_vali', password='Pass12345', role='teacher')
        self.teacher2 = Teacher.objects.create(
            user=self.user2, first_name='Vali', last_name='Aliyev',
            generated_login='t_face_vali', generated_password='Pass12345',
            face_embedding=json.dumps([0.50] * 128)
        )

    def test_strict_threshold_rejects_distant_face(self):
        """Masofa 0.45 dan katta bo'lsa (masalan 0.48 yoki 0.55), boshqa xodimga bog'lanmasligi kerak."""
        from core.face_service import compare_embeddings, match_staff_by_face

        # Skaner qilingan deskriptor: Ali'dan masofasi ~0.50 bo'lgan yuz
        # dist = sqrt(128 * (0.04419)^2) = sqrt(128 * 0.00195) ~ 0.50
        distant_desc = [0.10 + 0.0442] * 128
        score, is_match, dist = compare_embeddings([0.10] * 128, distant_desc)
        self.assertFalse(is_match)
        self.assertGreater(dist, 0.45)

        # match_staff_by_face begona/noaniq yuzni Aliga yoki Valiga bog'lab yubormasligi kerak
        match = match_staff_by_face(scan_descriptor=distant_desc)
        self.assertIsNone(match)

    def test_high_confidence_match_accepted(self):
        """Masofa juda yaqin bo'lganda (masalan dist <= 0.20), yuqori foiz bilan to'g'ri xodim aniqlanadi."""
        from core.face_service import compare_embeddings, match_staff_by_face

        # Skaner qilingan deskriptor: Ali'ga juda yaqin (masofa ~0.11)
        close_desc = [0.10 + 0.01] * 128
        score, is_match, dist = compare_embeddings([0.10] * 128, close_desc)
        self.assertTrue(is_match)
        self.assertLess(dist, 0.25)
        self.assertGreaterEqual(score, 90.0)

        match = match_staff_by_face(scan_descriptor=close_desc)
        self.assertIsNotNone(match)
        role, staff, match_score, match_dist = match
        self.assertEqual(staff.id, self.teacher1.id)
        self.assertGreaterEqual(match_score, 90.0)

    def test_ambiguity_guard_prevents_misidentification(self):
        """
        Agar ikki xodimning deskriptorlari deyarli bir xil masofada mos kelsa (farq juda kichik),
        tizim adashib xato xodimga yozib yubormaslik uchun rad etadi.
        """
        import json
        from core.face_service import match_staff_by_face

        # Ikki xodimni deyarli bir xil qilib qo'yamiz
        self.teacher1.face_embedding = json.dumps([0.10] * 128)
        self.teacher1.save()
        self.teacher2.face_embedding = json.dumps([0.102] * 128)
        self.teacher2.save()

        # Skan deskriptori: oraliqda (~0.40 masofada) joylashgan, farq < 0.02
        scan_desc = [0.10 + 0.035] * 128
        match = match_staff_by_face(scan_descriptor=scan_desc)
        # Ambiguity guard noaniq holatda xato xodimga yozmasligi kerak
        self.assertIsNone(match)

    def test_verify_api_rejects_conflicting_multiple_faces(self):
        """Kadrda bir nechta yuz aniqlanganda yoki raqobat bo'lganda API xatolik qaytaradi."""
        import json
        url = reverse('face_id_verify_api')
        payload = {
            'descriptor': [0.10] * 128,
            'is_kiosk': '1',
            'faces_count': 2,
            'face_conflict': True,
        }
        resp = self.client.post(url, data=json.dumps(payload), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data['success'])
        self.assertEqual(data['error_type'], 'multiple_faces')
        self.assertIn("bir nechta", data['message'].lower())



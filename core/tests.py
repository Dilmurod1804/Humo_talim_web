from datetime import date, datetime, time, timedelta
from django.test import TestCase, override_settings
from django.urls import reverse
from decimal import Decimal
from django.utils import timezone

from .models import (
    AdvanceRequest, Attendance, CustomUser, DeletedStudent, FaceIDLog, Group, Manager, MonthlyPayment,
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


class StudentPhotoPreviewAndMediaSettingsTests(TestCase):
    """O'quvchi rasmini ochuvchi modal, placeholder va MEDIA_URL sozlamalarini tekshirish."""

    def setUp(self):
        self.manager_user = CustomUser.objects.create_user(
            username='mgr_photo_test', password='Password123', role='manager'
        )
        self.manager = Manager.objects.create(
            user=self.manager_user, first_name='Menejer', last_name='Testov',
            generated_login='mgr_photo_test', generated_password='Password123'
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='tch_photo_test', password='Password123', role='teacher'
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user, first_name='Ustoz', last_name='Testova',
            generated_login='tch_photo_test', generated_password='Password123'
        )
        self.group = Group.objects.create(
            name='Fizika Guruh', subject='Fizika', teacher=self.teacher
        )
        self.timeslot = TimeSlot.objects.create(
            group=self.group, days='Dushanba-Chorshanba', start_time='10:00', end_time='12:00'
        )
        # 1. Rasmi bor o'quvchi
        from django.core.files.uploadedfile import SimpleUploadedFile
        test_photo = SimpleUploadedFile('student1.jpg', b'dummy_image_data', content_type='image/jpeg')
        self.student_with_photo = Student.objects.create(
            first_name='Anvar', last_name='Zokirov', phone_1='+998901112233',
            time_slot=self.timeslot, photo=test_photo
        )
        # 2. Rasmi yo'q o'quvchi
        self.student_without_photo = Student.objects.create(
            first_name='Botir', last_name='Komilov', phone_1='+998904445566',
            time_slot=self.timeslot, photo=None
        )

    def test_student_photo_url_property(self):
        """photo_url property rasmi bo'lsa URL qaytaradi, bo'lmasa xavfsiz bo'sh satr qaytaradi."""
        self.assertTrue(self.student_with_photo.photo_url.startswith('/media/'))
        self.assertEqual(self.student_without_photo.photo_url, '')

    def test_group_detail_renders_photo_and_placeholder_correctly(self):
        """Guruh sahifasida rasmi bor o'quvchiga <img>, rasmi yo'qqa esa placeholder chiqadi."""
        self.client.force_login(self.manager_user)
        resp = self.client.get(reverse('admin_group_detail', args=[self.group.id]))
        self.assertEqual(resp.status_code, 200)

        # 1. Rasmi bor o'quvchi uchun <img> va openImgPreview chaqiruvi bo'lishi kerak
        self.assertContains(resp, self.student_with_photo.photo.url)
        self.assertContains(resp, 'openImgPreview')

        # 2. Rasmi yo'q o'quvchi uchun placeholder class bo'lishi kerak
        self.assertContains(resp, 'student-avatar-thumb no-photo')

        # 3. imgPreviewModal da preview va placeholder elementlari mavjud bo'lishi kerak
        self.assertContains(resp, 'id="imgPreviewModal"')
        self.assertContains(resp, 'id="imgPreviewSrc"')
        self.assertContains(resp, 'id="imgPreviewPlaceholder"')

    def test_media_context_processor_in_templates(self):
        """django.template.context_processors.media sababli MEDIA_URL shablonda mavjud bo'ladi."""
        from django.template import Template, Context
        from django.test import RequestFactory

        rf = RequestFactory()
        request = rf.get('/')
        t = Template("MEDIA:{{ MEDIA_URL }}")
        c = Context({'request': request})
        rendered = t.render(c)
        self.assertIn('/media/', rendered)


class ChangeGroupTeacherTests(TestCase):
    """Guruh o'qituvchisini to'g'ridan-to'g'ri kartochkadan o'zgartirish funksiyasi testlari."""

    def setUp(self):
        self.manager_user = CustomUser.objects.create_user(
            username='mgr_grp_tch', password='Password123', role='manager'
        )
        self.manager = Manager.objects.create(
            user=self.manager_user, first_name='Aziz', last_name='Menejerov',
            generated_login='mgr_grp_tch', generated_password='Password123'
        )
        self.teacher_user1 = CustomUser.objects.create_user(
            username='tch_zamira', password='Password123', role='teacher'
        )
        self.teacher1 = Teacher.objects.create(
            user=self.teacher_user1, first_name='Zamira', last_name='Zakirova',
            generated_login='tch_zamira', generated_password='Password123'
        )
        self.teacher_user2 = CustomUser.objects.create_user(
            username='tch_bobur', password='Password123', role='teacher'
        )
        self.teacher2 = Teacher.objects.create(
            user=self.teacher_user2, first_name='Bobur', last_name='Rahimov',
            generated_login='tch_bobur', generated_password='Password123'
        )
        self.group = Group.objects.create(
            name='Ingliz tili B2', subject='Ingliz tili', teacher=self.teacher1
        )
        self.url = reverse('admin_group_change_teacher', args=[self.group.id])

    def test_manager_can_change_teacher_via_standard_post(self):
        """Menejer standart POST so'rovi orqali o'qituvchini o'zgartira oladi."""
        self.client.force_login(self.manager_user)
        response = self.client.post(self.url, {'teacher_id': str(self.teacher2.id)})
        self.assertEqual(response.status_code, 302)
        self.group.refresh_from_db()
        self.assertEqual(self.group.teacher, self.teacher2)

    def test_manager_can_change_teacher_via_ajax(self):
        """AJAX orqali so'rov yuborilganda JSON javob qaytadi va baza yangilanadi."""
        self.client.force_login(self.manager_user)
        response = self.client.post(
            self.url,
            {'teacher_id': str(self.teacher2.id), 'is_ajax': '1'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['teacher_name'], f"{self.teacher2.first_name} {self.teacher2.last_name}")
        self.assertEqual(data['teacher_id'], self.teacher2.id)

        self.group.refresh_from_db()
        self.assertEqual(self.group.teacher, self.teacher2)

    def test_unassign_teacher_to_none(self):
        """Bo'sh teacher_id yuborilganda o'qituvchi None (O'qituvchisiz) bo'ladi."""
        self.client.force_login(self.manager_user)
        response = self.client.post(
            self.url,
            {'teacher_id': '', 'is_ajax': '1'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertIsNone(data['teacher_id'])
        self.assertEqual(data['teacher_name'], "O'qituvchisiz")

        self.group.refresh_from_db()
        self.assertIsNone(self.group.teacher)

    def test_teacher_user_cannot_change_group_teacher(self):
        """Oddiy o'qituvchi guruh o'qituvchisini o'zgartira olmaydi (huquq cheklangan)."""
        self.client.force_login(self.teacher_user1)
        response = self.client.post(self.url, {'teacher_id': str(self.teacher2.id)})
        # @admin_required redirects teacher to teacher_dashboard
        self.assertRedirects(response, reverse('teacher_dashboard'))
        self.group.refresh_from_db()
        self.assertEqual(self.group.teacher, self.teacher1)

    def test_get_request_returns_active_teachers_list(self):
        """GET so'rovi guruh ma'lumotlari va faol o'qituvchilar ro'yxatini qaytaradi."""
        self.client.force_login(self.manager_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['group_name'], self.group.name)
        self.assertEqual(data['current_teacher_id'], self.teacher1.id)
        self.assertGreaterEqual(len(data['teachers']), 2)

    def test_admin_dashboard_renders_change_teacher_button_and_modal(self):
        """admin_dashboard sahifasida guruh kartochkasida o'qituvchini o'zgartirish tugmasi va modali mavjud."""
        self.client.force_login(self.manager_user)
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'id="groupTeacherName{self.group.id}"')
        self.assertContains(resp, 'openChangeTeacherModal')
        self.assertContains(resp, 'id="changeGroupTeacherModal"')


class ClickablePhoneLinkTests(TestCase):
    """
    O'quvchi va o'qituvchilarning telefon raqamlari tel: havolalari (clickable anchor tags)
    sifatida to'g'ri shakllanishi va shablonlarda aks etishini tekshirish.
    """
    def setUp(self):
        self.admin_user = CustomUser.objects.create_superuser(
            username='admin_phone_test', password='AdminPassword123', role='admin'
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='teacher_phone_test', password='TeacherPassword123', role='teacher'
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Zafar',
            last_name='Aliyev',
            phone='+998 90 123 45 67',
            generated_login='teacher_phone_test',
            generated_password='TeacherPassword123',
        )
        self.manager_user = CustomUser.objects.create_user(
            username='manager_phone_test', password='ManagerPassword123', role='manager'
        )
        self.manager = Manager.objects.create(
            user=self.manager_user,
            first_name='Anvar',
            last_name='Hakimov',
            phone='90 987 65 43',
            generated_login='manager_phone_test',
            generated_password='ManagerPassword123',
        )
        self.group = Group.objects.create(
            name='Matematika 1', subject='Matematika', teacher=self.teacher
        )
        self.timeslot = TimeSlot.objects.create(
            group=self.group, start_time='09:00', end_time='10:30', days='Dushanba, Chorshanba, Juma'
        )
        self.student = Student.objects.create(
            first_name='Jasur',
            last_name='Nematov',
            phone_1='+998 (91) 555-44-33',
            phone_2='93 111 22 33',
            time_slot=self.timeslot,
        )

    def test_format_phone_tel_helper(self):
        """format_phone_tel funksiyasi turli ko'rinishdagi telefonlarni xalqaro +998... formatga keltiradi."""
        from core.models import format_phone_tel
        self.assertEqual(format_phone_tel('+998901234567'), '+998901234567')
        self.assertEqual(format_phone_tel('+998 90 123 45 67'), '+998901234567')
        self.assertEqual(format_phone_tel('90 123 45 67'), '+998901234567')
        self.assertEqual(format_phone_tel('998901234567'), '+998901234567')
        self.assertEqual(format_phone_tel(''), '')
        self.assertEqual(format_phone_tel(None), '')

    def test_model_phone_properties(self):
        """Student, Teacher va Manager modellaridagi phone_tel propertylari to'g'ri tel: formatini qaytaradi."""
        self.assertEqual(self.student.phone_1_tel, '+998915554433')
        self.assertEqual(self.student.phone_2_tel, '+998931112233')
        self.assertEqual(self.teacher.phone_tel, '+998901234567')
        self.assertEqual(self.manager.phone_tel, '+998909876543')

    def test_phone_filters_templatetags(self):
        """phone_filters template teglari to'g'ri ishlaydi."""
        from core.templatetags.phone_filters import format_tel, phone_display
        self.assertEqual(format_tel('90 123 45 67'), '+998901234567')
        self.assertEqual(phone_display('901234567'), '+998 90 123 45 67')

    def test_admin_dashboard_renders_clickable_tel_links(self):
        """admin_dashboard sahifasida o'qituvchi va menejer telefonlari tel: havolasi bilan chiqadi."""
        self.client.force_login(self.admin_user)
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'href="tel:{self.teacher.phone_tel}"')
        self.assertContains(resp, f'href="tel:{self.manager.phone_tel}"')

    def test_admin_group_detail_renders_student_tel_link_and_quick_edit_call(self):
        """admin_group_detail sahifasida o'quvchi kartochkasida tel: havolasi va tez tahrirlashda qo'ng'iroq tugmasi bor."""
        self.client.force_login(self.admin_user)
        resp = self.client.get(reverse('admin_group_detail', args=[self.group.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f'href="tel:{self.student.phone_1_tel}"')
        self.assertContains(resp, 'id="ie_call_phone1"')
        self.assertContains(resp, 'id="ie_call_phone2"')


@override_settings(SMS_ENABLED=False, ESKIZ_API_TOKEN='', CENTER_NAME="Humo Ta'lim")
class SMSTuitionReminderTests(TestCase):
    """Oylik to'lov eslatmalari SMS xizmati, boshqaruv buyrug'i va admin view testlari.
    Testlarda haqiqiy SMS yuborilmasligi uchun SMS_ENABLED=False majburiy o'rnatiladi."""

    def setUp(self):
        from core.models import CustomUser, Teacher, Group, TimeSlot, Student, MonthlyPayment, SMSLog
        self.admin_user = CustomUser.objects.create_superuser(
            username='sms_admin', password='Password123'
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='sms_teacher', password='Password123', role='teacher'
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user, first_name='Nargiza', last_name='Alimova',
            generated_login='sms_teacher', generated_password='Password123'
        )
        self.group = Group.objects.create(
            name='Matematika A1', subject='Matematika', teacher=self.teacher
        )
        self.timeslot = TimeSlot.objects.create(
            group=self.group, start_time='14:00', end_time='15:30', days='Seshanba, Payshanba, Shanba'
        )
        # 1-o'quvchi: To'lov qilmagan (Qarzdor)
        self.student_unpaid = Student.objects.create(
            first_name='Anvar',
            last_name='Qodirov',
            phone_1='+998 (90) 111-22-33',
            time_slot=self.timeslot,
            payment_rate=400000,
        )
        # 2-o'quvchi: To'lov qilgan
        self.student_paid = Student.objects.create(
            first_name='Dilnoza',
            last_name='Karimova',
            phone_1='998909998877',
            time_slot=self.timeslot,
            payment_rate=400000,
        )
        self.current_month = timezone.now().strftime('%Y-%m')
        MonthlyPayment.objects.create(
            student=self.student_paid,
            month=self.current_month,
            amount_paid='400000',
            status='paid',
        )

    def test_clean_phone_number(self):
        """Telefon raqamini 12 xonali xalqaro (998XXXXXXXXX) formatga keltirish tekshiruvi."""
        from core.sms_service import clean_phone_number
        self.assertEqual(clean_phone_number('+998 (90) 123-45-67'), '998901234567')
        self.assertEqual(clean_phone_number('90 123 45 67'), '998901234567')
        self.assertEqual(clean_phone_number('998901234567'), '998901234567')
        self.assertEqual(clean_phone_number(''), '')
        self.assertEqual(clean_phone_number(None), '')

    def test_build_tuition_reminder_text(self):
        """O'zbekcha (lotin) professional to'lov eslatmasi matni to'g'ri shakllanadi."""
        from core.sms_service import build_tuition_reminder_text
        text = build_tuition_reminder_text(self.student_unpaid, month_str=self.current_month)
        self.assertIn("Hurmatli ota-ona!", text)
        self.assertIn("Anvar Qodirov", text)
        self.assertIn("Matematika", text)
        self.assertIn("400 000", text)
        self.assertIn("Humo Ta'lim", text)

    def test_send_sms_mock_simulation(self):
        """SMS_ENABLED=False bo'lganda SMS simulyatsiya (mock) rejimida muvaffaqiyatli ishlaydi."""
        from core.sms_service import send_sms
        res = send_sms('998901112233', 'Sinov xabari')
        self.assertTrue(res['success'])
        self.assertEqual(res['status'], 'mock')
        self.assertTrue(res['message_id'].startswith('mock_'))

    def test_send_tuition_reminder_creates_log_and_prevents_duplicate(self):
        """To'lov eslatmasi SMSLog ga yoziladi va bitta oyda qayta takror yuborilishi oldi olinadi."""
        from core.sms_service import send_tuition_reminder
        from core.models import SMSLog

        # 1-marta yuborish
        res1 = send_tuition_reminder(self.student_unpaid, month_str=self.current_month, sent_by=self.admin_user)
        self.assertTrue(res1['success'])
        self.assertEqual(res1['status'], 'mock')
        self.assertEqual(SMSLog.objects.filter(student=self.student_unpaid, month=self.current_month).count(), 1)

        # 2-marta yuborish (force=False) -> o'tkazib yuborilishi kerak
        res2 = send_tuition_reminder(self.student_unpaid, month_str=self.current_month, sent_by=self.admin_user, force=False)
        self.assertTrue(res2['success'])
        self.assertEqual(res2['status'], 'skipped')
        # Yangi log yaratilmasligi kerak
        self.assertEqual(SMSLog.objects.filter(student=self.student_unpaid, month=self.current_month).count(), 1)

        # 3-marta yuborish (force=True) -> qayta yuboriladi
        res3 = send_tuition_reminder(self.student_unpaid, month_str=self.current_month, sent_by=self.admin_user, force=True)
        self.assertTrue(res3['success'])
        self.assertEqual(SMSLog.objects.filter(student=self.student_unpaid, month=self.current_month).count(), 2)

    def test_send_tuition_reminders_batch_filters_unpaid_only(self):
        """Ommaviy eslatma faqat to'lov qilmagan (qarzdor) o'quvchilarga yuboriladi."""
        from core.sms_service import send_tuition_reminders_batch
        from core.models import SMSLog

        summary = send_tuition_reminders_batch(month_str=self.current_month, sent_by=self.admin_user)
        # self.student_paid to'lagan, faqat self.student_unpaid ga yuboriladi
        self.assertGreaterEqual(summary['total_unpaid'], 1)
        self.assertEqual(summary['sent'], 1)
        self.assertTrue(SMSLog.objects.filter(student=self.student_unpaid, month=self.current_month).exists())
        self.assertFalse(SMSLog.objects.filter(student=self.student_paid, month=self.current_month).exists())

    def test_management_command_send_tuition_reminders(self):
        """python manage.py send_tuition_reminders komandasi parametrlar bilan ishlaydi."""
        from django.core.management import call_command
        from io import StringIO

        out = StringIO()
        call_command(
            'send_tuition_reminders',
            month=self.current_month,
            group=str(self.group.id),
            dry_run=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertIn("DRY-RUN", output)
        self.assertIn("Matematika", output)

    def test_admin_send_payment_reminders_view_standard_post(self):
        """Admin view orqali standart POST so'rovda eslatmalar yuboriladi va sahifaga yo'naltiriladi."""
        self.client.force_login(self.admin_user)
        url = reverse('admin_send_payment_reminders')
        resp = self.client.post(url, {
            'month': self.current_month,
            'group_id': str(self.group.id),
        })
        self.assertEqual(resp.status_code, 302)

    def test_admin_send_payment_reminders_view_ajax(self):
        """Admin view AJAX so'rovida JSON formatda summary qaytaradi."""
        self.client.force_login(self.admin_user)
        url = reverse('admin_send_payment_reminders')
        resp = self.client.post(
            url,
            {'month': self.current_month, 'group_id': str(self.group.id)},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertIn('summary', data)
        self.assertIn('sent', data['summary'])

    def _fake_response(self, status_code, payload):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = payload
        resp.text = str(payload)
        return resp

    def test_send_sms_eskiz_uses_bearer_token_from_settings(self):
        """SMS_ENABLED=True bo'lganda settings.ESKIZ_API_TOKEN bilan Eskiz API ga to'g'ri POST yuboriladi."""
        from unittest.mock import patch
        from django.core.cache import cache
        from core import sms_service
        cache.delete(sms_service.ESKIZ_CACHE_KEY)

        with override_settings(SMS_ENABLED=True, SMS_PROVIDER='eskiz', ESKIZ_API_TOKEN='TEST_TOKEN', ESKIZ_FROM='4546'):
            with patch.object(sms_service.requests, 'post') as mock_post:
                mock_post.return_value = self._fake_response(200, {'id': 'abc-123', 'status': 'waiting', 'message': 'Waiting for SMS provider'})
                res = sms_service.send_sms('+998 90 111 22 33', 'Salom')

        self.assertTrue(res['success'])
        self.assertEqual(res['status'], 'sent')
        self.assertEqual(res['message_id'], 'abc-123')
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'https://notify.eskiz.uz/api/message/sms/send')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer TEST_TOKEN')
        self.assertEqual(kwargs['data'], {'mobile_phone': '998901112233', 'message': 'Salom', 'from': '4546'})

    def test_send_sms_eskiz_401_without_credentials_returns_clear_error(self):
        """Token yaroqsiz (401) bo'lsa va login ma'lumotlari bo'lmasa, tushunarli xato qaytadi."""
        from unittest.mock import patch
        from django.core.cache import cache
        from core import sms_service
        cache.delete(sms_service.ESKIZ_CACHE_KEY)

        with override_settings(SMS_ENABLED=True, SMS_PROVIDER='eskiz', ESKIZ_API_TOKEN='EXPIRED', ESKIZ_EMAIL='', ESKIZ_PASSWORD=''):
            with patch.object(sms_service.requests, 'post') as mock_post:
                mock_post.return_value = self._fake_response(401, {'message': 'Expired'})
                res = sms_service.send_sms('901112233', 'Salom')

        self.assertFalse(res['success'])
        self.assertEqual(res['status'], 'failed')
        self.assertIn('401', res['error'])
        self.assertEqual(mock_post.call_count, 1)

    def test_get_token_expiry_decodes_jwt(self):
        """JWT tokendagi exp maydoni to'g'ri o'qiladi."""
        import base64, json as _json
        from core.sms_service import get_token_expiry
        payload = base64.urlsafe_b64encode(_json.dumps({'exp': 1796630005}).encode()).decode().rstrip('=')
        expiry = get_token_expiry(f"xxx.{payload}.yyy")
        self.assertIsNotNone(expiry)
        self.assertEqual(int(expiry.timestamp()), 1796630005)
        self.assertIsNone(get_token_expiry('not-a-jwt'))


class MonthlyAttendanceCalculationTests(TestCase):
    """O'quvchi, Guruh va O'qituvchi oylik davomat foizlari hamda maosh integratsiyasi testlari."""

    def setUp(self):
        self.admin_user = CustomUser.objects.create_superuser(
            username='admin_att_test', password='Password123'
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='teacher_att_test', password='Password123', role='teacher'
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Malika',
            last_name='Nazarova',
            generated_login='teacher_att_test',
            generated_password='Password123',
            monthly_salary=Decimal('4000000.00'),
        )
        # Guruh 1
        self.group1 = Group.objects.create(
            name='Matematika 1',
            subject='Matematika',
            teacher=self.teacher,
        )
        self.ts1 = TimeSlot.objects.create(
            group=self.group1,
            days='Dushanba, Chorshanba, Juma',
            start_time='09:00',
            end_time='10:30',
        )
        # Guruh 2
        self.group2 = Group.objects.create(
            name='Matematika 2',
            subject='Matematika',
            teacher=self.teacher,
        )
        self.ts2 = TimeSlot.objects.create(
            group=self.group2,
            days='Seshanba, Payshanba, Shanba',
            start_time='11:00',
            end_time='12:30',
        )

        # Guruh 1 dagi o'quvchilar
        self.student1 = Student.objects.create(
            first_name='Anvar',
            last_name='Sobirov',
            phone_1='998901111111',
            time_slot=self.ts1,
            joined_date=date(2026, 10, 1),
        )
        self.student2 = Student.objects.create(
            first_name='Feruza',
            last_name='Kamalova',
            phone_1='998902222222',
            time_slot=self.ts1,
            joined_date=date(2026, 10, 1),
        )
        # Guruh 2 dagi o'quvchi
        self.student3 = Student.objects.create(
            first_name='Bobur',
            last_name='Alimov',
            phone_1='998903333333',
            time_slot=self.ts2,
            joined_date=date(2026, 10, 1),
        )

        # 2026-yil Oktyabr oyi uchun test ma'lumotlari:
        # student1: 8 ta present, 2 ta absent => 8/10 = 80.0%
        # student2: 10 ta present, 0 ta absent => 10/10 = 100.0%
        # group1 jami: 18 ta present, 2 ta absent => 18/20 = 90.0%
        # student3: 5 ta present, 5 ta absent => 5/10 = 50.0% (group2)
        # teacher jami: (18 + 5) / (20 + 10) = 23 / 30 = 76.67%
        records = []
        # student1 records
        for day in range(1, 9):  # 8 present
            records.append(Attendance(student=self.student1, date=date(2026, 10, day), status='present'))
        for day in range(9, 11):  # 2 absent
            records.append(Attendance(student=self.student1, date=date(2026, 10, day), status='absent'))

        # student2 records
        for day in range(1, 11):  # 10 present
            records.append(Attendance(student=self.student2, date=date(2026, 10, day), status='present'))

        # student3 records (Group 2)
        for day in range(1, 6):  # 5 present
            records.append(Attendance(student=self.student3, date=date(2026, 10, day), status='present'))
        for day in range(6, 11):  # 5 absent
            records.append(Attendance(student=self.student3, date=date(2026, 10, day), status='absent'))

        Attendance.objects.bulk_create(records)

    def test_student_monthly_attendance_calculation(self):
        """Yakka o'quvchining oylik davomati foizi to'g'ri hisoblanadi."""
        from core.attendance_service import calculate_student_monthly_attendance

        # student1: 8 present, 2 absent => 80.0%
        res1 = calculate_student_monthly_attendance(self.student1, year=2026, month=10)
        self.assertEqual(res1['present_classes'], 8)
        self.assertEqual(res1['absent_classes'], 2)
        self.assertEqual(res1['total_classes'], 10)
        self.assertEqual(res1['percentage'], 80.0)

        # Model metodi orqali ham to'g'ri natija qaytadi
        self.assertEqual(self.student1.get_monthly_attendance_percentage(2026, 10), 80.0)

        # student2: 10 present, 0 absent => 100.0%
        res2 = calculate_student_monthly_attendance(self.student2, year=2026, month=10)
        self.assertEqual(res2['present_classes'], 10)
        self.assertEqual(res2['total_classes'], 10)
        self.assertEqual(res2['percentage'], 100.0)

    def test_student_monthly_attendance_zero_classes_edge_case(self):
        """Yo'qlama qilinmagan oy uchun ZeroDivisionError bermasdan 0.0% qaytadi."""
        from core.attendance_service import calculate_student_monthly_attendance

        # 2026-yil Noyabr oyida hali davomat qilinmagan
        res = calculate_student_monthly_attendance(self.student1, year=2026, month=11)
        self.assertEqual(res['present_classes'], 0)
        self.assertEqual(res['total_classes'], 0)
        self.assertEqual(res['percentage'], 0.0)
        self.assertFalse(res['has_records'])

    def test_group_monthly_attendance_calculation(self):
        """Guruhning oylik umumiy davomat foizi to'g'ri hisoblanadi."""
        from core.attendance_service import calculate_group_monthly_attendance

        # Group 1: 18 present / 20 total = 90.0%
        grp1_res = calculate_group_monthly_attendance(self.group1, year=2026, month=10)
        self.assertEqual(grp1_res['present_records'], 18)
        self.assertEqual(grp1_res['absent_records'], 2)
        self.assertEqual(grp1_res['total_records'], 20)
        self.assertEqual(grp1_res['percentage'], 90.0)
        self.assertEqual(grp1_res['students_count'], 2)
        self.assertEqual(grp1_res['lesson_days_count'], 10)

        # Model metodi
        self.assertEqual(self.group1.get_monthly_attendance_percentage(2026, 10), 90.0)

        # Group 2: 5 present / 10 total = 50.0%
        grp2_res = calculate_group_monthly_attendance(self.group2, year=2026, month=10)
        self.assertEqual(grp2_res['percentage'], 50.0)
        self.assertEqual(self.group2.get_monthly_attendance_percentage(2026, 10), 50.0)

    def test_group_monthly_attendance_zero_records_edge_case(self):
        """Yo'qlama yo'q oy uchun guruh davomati 0.0% qaytadi."""
        from core.attendance_service import calculate_group_monthly_attendance

        res = calculate_group_monthly_attendance(self.group1, year=2026, month=11)
        self.assertEqual(res['total_records'], 0)
        self.assertEqual(res['percentage'], 0.0)
        self.assertFalse(res['has_records'])

    def test_teacher_monthly_attendance_calculation(self):
        """O'qituvchining barcha guruhlari bo'yicha umumiy davomat ko'rsatkichi hisoblanadi."""
        from core.attendance_service import calculate_teacher_monthly_attendance

        # Jami: (18 + 5) / (20 + 10) = 23 / 30 = 76.67%
        res = calculate_teacher_monthly_attendance(self.teacher, year=2026, month=10)
        self.assertEqual(res['groups_count'], 2)
        self.assertEqual(res['total_records'], 30)
        self.assertEqual(res['present_records'], 23)
        self.assertEqual(res['absent_records'], 7)
        self.assertEqual(res['overall_percentage'], 76.67)

        # Model metodi
        self.assertEqual(self.teacher.get_monthly_attendance_percentage(2026, 10), 76.67)

    def test_teacher_attendance_kpi_evaluation_and_bonus(self):
        """O'qituvchi davomat KPI bahosi va bonus huquqi to'g'ri aniqlanadi."""
        from core.attendance_service import evaluate_teacher_attendance_kpi, apply_teacher_attendance_bonus
        from core.models import StaffBonus

        # 76.67% bo'lgani uchun AVERAGE daraja (70% - 79.9%)
        kpi = evaluate_teacher_attendance_kpi(self.teacher, year=2026, month=10)
        self.assertEqual(kpi['kpi_status'], 'AVERAGE')
        self.assertFalse(kpi['is_bonus_eligible'])

        # Faqat Group 1 ni hisobga olsak (90.0% => EXCELLENT)
        # Guruh 2 ni o'chirib yoki 90%+ davomatli o'qituvchi bilan sinab ko'ramiz
        teacher2_user = CustomUser.objects.create_user(username='t_exc', password='Password123', role='teacher')
        teacher2 = Teacher.objects.create(
            user=teacher2_user, first_name='Zilola', last_name='Ahmedova',
            monthly_salary=Decimal('5000000.00'),
        )
        self.group1.teacher = teacher2
        self.group1.save()

        kpi2 = evaluate_teacher_attendance_kpi(teacher2, year=2026, month=10)
        self.assertEqual(kpi2['kpi_status'], 'EXCELLENT')
        self.assertTrue(kpi2['is_bonus_eligible'])
        # 5% of 5,000,000 = 250,000
        self.assertEqual(kpi2['suggested_bonus'], Decimal('250000.00'))

        # Bonusni bazaga biriktirish (apply_teacher_attendance_bonus)
        bonus_obj, created, msg = apply_teacher_attendance_bonus(teacher2, year=2026, month=10, created_by=self.admin_user)
        self.assertTrue(created)
        self.assertIsNotNone(bonus_obj)
        self.assertEqual(bonus_obj.amount, Decimal('250000.00'))
        self.assertEqual(StaffBonus.objects.filter(user=teacher2_user, month='2026-10').count(), 1)

        # Takroriy qo'shish urinishi (spam protection) -> created=False
        bonus_obj2, created2, msg2 = apply_teacher_attendance_bonus(teacher2, year=2026, month=10)
        self.assertFalse(created2)
        self.assertEqual(StaffBonus.objects.filter(user=teacher2_user, month='2026-10').count(), 1)

    def test_admin_salary_report_view_includes_attendance_kpi(self):
        """admin_salary_report sahifasida o'qituvchilarning davomat ma'lumotlari to'g'ri ko'rinadi."""
        self.client.force_login(self.admin_user)
        resp = self.client.get(reverse('admin_salary_report') + '?month=2026-10')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Davomat:")

    def test_admin_group_detail_displays_attendance_percentages(self):
        """admin_group_detail sahifasida guruh va har bir o'quvchi davomat foizlari to'g'ri hisoblanadi va ko'rsatiladi."""
        self.client.force_login(self.admin_user)
        resp = self.client.get(reverse('admin_group_detail', args=[self.group1.id]) + '?month=2026-10')
        self.assertEqual(resp.status_code, 200)

        # Context tekshiruvi: Guruh umumiy davomati
        group_att = resp.context['group_attendance']
        self.assertEqual(group_att['total_records'], 20)
        self.assertEqual(group_att['present_records'], 18)
        self.assertEqual(group_att['absent_records'], 2)
        self.assertEqual(group_att['percentage'], 90.0)
        self.assertEqual(resp.context['group_attendance_percentage'], 90.0)

        # O'quvchilar alohida foizlari
        active_students = resp.context['timeslots'][0].active_students
        st_map = {s.id: s for s in active_students}
        # student1: 8/10 = 80.0%
        self.assertEqual(st_map[self.student1.id].attendance_percentage, 80.0)
        self.assertEqual(st_map[self.student1.id].attendance_present_count, 8)
        self.assertEqual(st_map[self.student1.id].attendance_total_count, 10)
        # student2: 10/10 = 100.0%
        self.assertEqual(st_map[self.student2.id].attendance_percentage, 100.0)
        self.assertEqual(st_map[self.student2.id].attendance_present_count, 10)
        self.assertEqual(st_map[self.student2.id].attendance_total_count, 10)

        # HTML sahifasida guruh va o'quvchilar foizlari aks etganligi (aniq raqamli ko'rsatkich formatida)
        self.assertContains(resp, "Guruh davomati:")
        self.assertContains(resp, "80% (8/10)")
        self.assertContains(resp, "100% (10/10)")
        self.assertContains(resp, "(18/20)")

    def test_admin_group_detail_zero_records_edge_case(self):
        """Yo'qlama yo'q oyda guruh va o'quvchilar davomati xatosiz 0% ko'rsatiladi."""
        self.client.force_login(self.admin_user)
        # 2026-11 oyida davomat qaydlari yo'q
        resp = self.client.get(reverse('admin_group_detail', args=[self.group1.id]) + '?month=2026-11')
        self.assertEqual(resp.status_code, 200)

        group_att = resp.context['group_attendance']
        self.assertEqual(group_att['total_records'], 0)
        self.assertEqual(group_att['percentage'], 0.0)

        active_students = resp.context['timeslots'][0].active_students
        for s in active_students:
            self.assertEqual(s.attendance_percentage, 0.0)
            self.assertEqual(s.attendance_total_count, 0)

        self.assertContains(resp, "0% (0/0)")
        self.assertNotContains(resp, "100% (")

    def test_teacher_dashboard_displays_attendance_percentages(self):
        """teacher_dashboard sahifasida ham guruh va o'quvchilar davomat foizlari to'g'ri chiqadi."""
        self.client.force_login(self.teacher_user)
        resp = self.client.get(reverse('teacher_dashboard') + f'?group_id={self.group1.id}&month=2026-10')
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(resp.context['group_attendance_percentage'], 90.0)
        self.assertContains(resp, "Davomat:")
        self.assertContains(resp, "80% (8/10)")
        self.assertContains(resp, "100% (10/10)")
        self.assertContains(resp, "(18/20)")

    def test_teacher_dashboard_zero_records_not_100_percent(self):
        """Yo'qlama bo'lmagan oyda teacher_dashboard da 0% (0/0) chiqadi, 100% emas."""
        self.client.force_login(self.teacher_user)
        resp = self.client.get(reverse('teacher_dashboard') + f'?group_id={self.group1.id}&month=2026-11')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['group_attendance_percentage'], 0.0)
        self.assertContains(resp, "0% (0/0)")
        self.assertNotContains(resp, "100% (")

    def test_compute_student_attendance_stats_empty_and_invalid(self):
        """Bo'sh yoki noto'g'ri statusli davomat uchun foiz 0.0 qaytariladi (100% emas)."""
        from core.attendance_service import compute_student_attendance_stats

        empty_stats = compute_student_attendance_stats({})
        self.assertEqual(empty_stats['percentage'], 0.0)
        self.assertEqual(empty_stats['total_count'], 0)
        self.assertFalse(empty_stats['has_records'])

        invalid_stats = compute_student_attendance_stats({1: '', 2: 'unknown'})
        self.assertEqual(invalid_stats['percentage'], 0.0)
        self.assertEqual(invalid_stats['total_count'], 0)
        self.assertFalse(invalid_stats['has_records'])

    def test_attendance_mark_api_returns_0_percent_when_cleared(self):
        """AJAX orqali davomat belgilanganda va o'chirilganda API to'g'ri 0% (0/0) qaytaradi."""
        today_str = date.today().strftime('%Y-%m-%d')
        self.client.force_login(self.admin_user)

        # 1. Present deb belgilash -> 100% (1/1)
        resp1 = self.client.post(reverse('admin_attendance_mark'), {
            'student_id': self.student1.id,
            'date': today_str,
            'status': 'present',
        })
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertEqual(data1['student_attendance']['percentage'], 100.0)
        self.assertEqual(data1['student_attendance']['display'], '100% (1/1)')

        # 2. Belgini tozalash (status='') -> 0% (0/0) qaytishi kerak (100% emas!)
        resp2 = self.client.post(reverse('admin_attendance_mark'), {
            'student_id': self.student1.id,
            'date': today_str,
            'status': '',
        })
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(data2['student_attendance']['percentage'], 0.0)
        self.assertEqual(data2['student_attendance']['total_count'], 0)
        self.assertEqual(data2['student_attendance']['display'], '0% (0/0)')

    def test_unmarked_blank_days_are_ignored_in_student_attendance(self):
        """
        Bo'sh (belgilanmagan) kunlar hech qachon 'present' (+) yoki 'absent' (-) sifatida
        hisoblanmasligi kerak. Maxraj qat'iy ravishda faqat shu o'quvchi uchun haqiqiy
        belgilangan ('present' + 'absent') kunlar sonidan iborat bo'ladi.
        """
        partial_student = Student.objects.create(
            first_name='Sardor',
            last_name='Qisman',
            phone_1='998909998877',
            time_slot=self.ts1,
            joined_date=date(2026, 10, 1),
        )
        # ts1 da 2026-10-01..2026-10-10 gacha 10 ta dars kuni bor (setUp da yaratilgan).
        # O'quvchida 2 ta 'present' (+) va 3 ta 'absent' (-) mavjud, qolgan 5 kuni bo'sh.
        # Formula: 2 / (2 + 3) * 100 = 40.0% (bo'sh kunlar present ham, absent ham emas).
        Attendance.objects.bulk_create([
            Attendance(student=partial_student, date=date(2026, 10, 1), status='present'),
            Attendance(student=partial_student, date=date(2026, 10, 2), status='present'),
            Attendance(student=partial_student, date=date(2026, 10, 3), status='absent'),
            Attendance(student=partial_student, date=date(2026, 10, 4), status='absent'),
            Attendance(student=partial_student, date=date(2026, 10, 5), status='absent'),
        ])

        # 1. Backend calculate_student_monthly_attendance funksiyasi
        from core.attendance_service import calculate_student_monthly_attendance
        stats = calculate_student_monthly_attendance(partial_student, 2026, 10)
        self.assertEqual(stats['present_classes'], 2)
        self.assertEqual(stats['absent_classes'], 3)
        self.assertEqual(stats['total_classes'], 5)
        self.assertEqual(stats['percentage'], 40.0)

        # 2. admin_group_detail sahifasi
        self.client.force_login(self.admin_user)
        resp_admin = self.client.get(reverse('admin_group_detail', args=[self.group1.id]) + '?month=2026-10')
        self.assertEqual(resp_admin.status_code, 200)
        self.assertContains(resp_admin, '40% (2/5)')

        # 3. teacher_dashboard sahifasi
        self.client.force_login(self.teacher_user)
        resp_teacher = self.client.get(reverse('teacher_dashboard') + f'?group_id={self.group1.id}&month=2026-10')
        self.assertEqual(resp_teacher.status_code, 200)
        self.assertContains(resp_teacher, '40% (2/5)')

        # 4. Excel eksport funksiyasi
        self.client.force_login(self.admin_user)
        resp_excel = self.client.get(reverse('admin_group_export_excel', args=[self.group1.id]) + '?month=2026-10')
        self.assertEqual(resp_excel.status_code, 200)
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(resp_excel.content))
        ws = wb.active
        partial_row_values = []
        for row in ws.iter_rows(min_row=4, values_only=True):
            if row and row[1] and 'Sardor' in str(row[1]):
                partial_row_values = list(row)
                break
        self.assertTrue(partial_row_values, "O'quvchi Excel jadvalida topilmadi")
        self.assertEqual(partial_row_values[5], '40%')
        self.assertEqual(partial_row_values[6], '2/5')

        # 5. Belgilangan yozuv bo'lmagan oyda (2026-11) 0% (0/0) qaytadi (100% yoki NaN emas)
        stats_nov = calculate_student_monthly_attendance(partial_student, 2026, 11)
        self.assertEqual(stats_nov['present_classes'], 0)
        self.assertEqual(stats_nov['absent_classes'], 0)
        self.assertEqual(stats_nov['total_classes'], 0)
        self.assertEqual(stats_nov['percentage'], 0.0)


class GroupAttendanceExcelExportTests(TestCase):
    """Guruh oylik davomatini Excel eksport qilish funksiyasi testlari."""

    def setUp(self):
        self.admin_user = CustomUser.objects.create_superuser(
            username='admin_excel_test', password='Password123'
        )
        self.teacher_user = CustomUser.objects.create_user(
            username='teacher_excel_test', password='Password123', role='teacher'
        )
        self.teacher = Teacher.objects.create(
            user=self.teacher_user,
            first_name='Shahnoza',
            last_name='Karimova',
            generated_login='teacher_excel_test',
            generated_password='Password123',
        )
        self.group = Group.objects.create(
            name='Fizika 101',
            subject='Fizika',
            teacher=self.teacher,
        )
        self.ts = TimeSlot.objects.create(
            group=self.group,
            days='Dushanba, Chorshanba, Juma',
            start_time='14:00',
            end_time='15:30',
        )
        self.student1 = Student.objects.create(
            first_name='Jasur',
            last_name='Toshmatov',
            phone_1='+998901234567',
            payment_rate=450000,
            time_slot=self.ts,
            joined_date=date(2026, 10, 1),
            is_active=True,
        )
        self.student2 = Student.objects.create(
            first_name='Dilnoza',
            last_name='Ergasheva',
            phone_1='+998939876543',
            payment_rate=450000,
            time_slot=self.ts,
            joined_date=date(2026, 10, 1),
            is_active=True,
        )
        # 2026 yil oktyabr (31 kun) uchun davomat:
        # student1: 8 kelgan (+), 2 kelmagan (-) -> 80% (8/10)
        records = []
        oct_days = [1, 2, 5, 7, 9, 12, 14, 16, 19, 21]
        for idx, day in enumerate(oct_days):
            st1_status = 'absent' if idx in (8, 9) else 'present'
            records.append(Attendance(
                student=self.student1,
                date=date(2026, 10, day),
                status=st1_status,
            ))
            # student2: barchasiga kelgan (+) -> 100% (10/10)
            records.append(Attendance(
                student=self.student2,
                date=date(2026, 10, day),
                status='present',
            ))
        Attendance.objects.bulk_create(records)

    def test_admin_group_export_excel_success(self):
        """Admin guruh sahifasidagi Excel eksport dinamik sana ustunlari va ko'rsatkichlarni to'g'ri shakllantiradi."""
        import openpyxl
        from io import BytesIO

        self.client.force_login(self.admin_user)
        resp = self.client.get(reverse('admin_group_export_excel', args=[self.group.id]) + '?month=2026-10')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        self.assertIn('Davomat_Fizika_101_2026-10.xlsx', resp['Content-Disposition'])

        # Excel faylni o'qish
        wb = openpyxl.load_workbook(BytesIO(resp.content))
        ws = wb.active

        # Sarlavha satrlari
        self.assertIn("FIZIKA 101 GURUHI DAVOMAT JURNALI", str(ws['A1'].value))
        self.assertIn("2026-10", str(ws['A2'].value))

        # Jadval ustun sarlavhalari (Row 3)
        headers = [ws.cell(row=3, column=c).value for c in range(1, 39)]
        self.assertEqual(headers[0], "№")
        self.assertEqual(headers[1], "F.I.O")
        self.assertEqual(headers[2], "Telefon raqami")
        self.assertEqual(headers[3], "Kelgan sana")
        self.assertEqual(headers[4], "To'lov tarifi")
        self.assertEqual(headers[5], "Davomat foizi")
        self.assertEqual(headers[6], "Keldi / Jami")

        # Oktyabr oyining barcha 31 kuni dinamik ustun sifatida mavjudligi
        self.assertEqual(headers[7], "01.10")
        self.assertEqual(headers[8], "02.10")
        self.assertEqual(headers[37], "31.10")
        self.assertEqual(len(headers), 7 + 31)

        # O'quvchilar qatorlari (Row 4 va Row 5)
        # O'quvchilar ism-familiyasi bo'yicha saralangan: Dilnoza, Jasur
        row4_name = ws.cell(row=4, column=2).value
        row5_name = ws.cell(row=5, column=2).value

        if "Dilnoza" in row4_name:
            dilnoza_row, jasur_row = 4, 5
        else:
            dilnoza_row, jasur_row = 5, 4

        # Dilnoza: 100% va 10/10
        self.assertEqual(ws.cell(row=dilnoza_row, column=6).value, "100%")
        self.assertEqual(ws.cell(row=dilnoza_row, column=7).value, "10/10")

        # Jasur: 80% va 8/10
        self.assertEqual(ws.cell(row=jasur_row, column=6).value, "80%")
        self.assertEqual(ws.cell(row=jasur_row, column=7).value, "8/10")

        # Jasurning kunlik belgilari tekshiruvi:
        # 1-oktyabr (ustun 8) -> '+'
        self.assertEqual(ws.cell(row=jasur_row, column=8).value, "+")
        # 21-oktyabr (ustun 7 + 21 = 28) -> '-' (absent)
        self.assertEqual(ws.cell(row=jasur_row, column=28).value, "-")
        # Dars o'tkazilmagan kun, masalan 3-oktyabr (ustun 10) -> bo'sh katak (None yoki "")
        self.assertIn(ws.cell(row=jasur_row, column=10).value, (None, ""))

        # Jami / Xulosa qatori (Row 6)
        self.assertEqual(ws.cell(row=6, column=2).value, "JAMI / GURUH DAVOMATI:")
        self.assertEqual(ws.cell(row=6, column=6).value, "90%")
        self.assertEqual(ws.cell(row=6, column=7).value, "18/20")

    def test_teacher_export_excel_with_group_attendance(self):
        """O'qituvchi o'z kabinetidan guruh bo'yicha Excel eksport qilganda ham yo'qlama jurnali chiqadi."""
        import openpyxl
        from io import BytesIO

        self.client.force_login(self.teacher_user)
        resp = self.client.get(reverse('teacher_export_excel') + f'?group_id={self.group.id}&month=2026-10')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Davomat_Fizika_101_2026-10.xlsx', resp['Content-Disposition'])

        wb = openpyxl.load_workbook(BytesIO(resp.content))
        ws = wb.active
        self.assertIn("FIZIKA 101 GURUHI DAVOMAT JURNALI", str(ws['A1'].value))

    def test_admin_group_export_excel_zero_records_edge_case(self):
        """Davomat qilinmagan oy uchun Excel xatoliksiz 0% va bo'sh kataklar bilan yuklanadi."""
        import openpyxl
        from io import BytesIO

        self.client.force_login(self.admin_user)
        # 2026-11 oyi uchun davomat mavjud emas
        resp = self.client.get(reverse('admin_group_export_excel', args=[self.group.id]) + '?month=2026-11')
        self.assertEqual(resp.status_code, 200)

        wb = openpyxl.load_workbook(BytesIO(resp.content))
        ws = wb.active
        # Noyabr (30 kun): 7 + 30 = 37 ustun
        self.assertEqual(ws.cell(row=3, column=37).value, "30.11")
        # O'quvchi davomati 0% va 0/0
        self.assertEqual(ws.cell(row=4, column=6).value, "0%")
        self.assertEqual(ws.cell(row=4, column=7).value, "0/0")
        self.assertEqual(ws.cell(row=6, column=6).value, "0%")
        self.assertEqual(ws.cell(row=6, column=7).value, "0/0")

    def test_admin_group_export_excel_permission_denied(self):
        """Tizimga kirmagan foydalanuvchi Excelni yuklay olmaydi."""
        resp = self.client.get(reverse('admin_group_export_excel', args=[self.group.id]))
        # admin_required login yoki home sahifasiga yo'naltiradi
        self.assertEqual(resp.status_code, 302)








from django.test import TestCase
from django.urls import reverse

from .models import AdvanceRequest, CustomUser, Group, Manager, MonthlyPayment, Student, Teacher, TimeSlot


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


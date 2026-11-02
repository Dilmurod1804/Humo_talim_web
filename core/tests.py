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

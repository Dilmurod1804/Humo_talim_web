"""
Django Management Command: send_paid_student_reminders
Faqat to'lovli o'quvchilarga (student_type='paid', is_active=True) Eskiz API orqali to'lov eslatmasi yuborish.
Mahalla / Imtiyozli o'quvchilar mutlaqo o'tkazib yuboriladi va ularga SMS bormaydi.

Foydalanish:
    python manage.py send_paid_student_reminders
    python manage.py send_paid_student_reminders --month 2026-11
    python manage.py send_paid_student_reminders --group 3
    python manage.py send_paid_student_reminders --dry-run
    python manage.py send_paid_student_reminders --force
    python manage.py send_paid_student_reminders --check-token
    python manage.py send_paid_student_reminders --test-phone 998901234567
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.eskiz_service import (
    send_payment_reminders_to_paid_students,
    send_test_payment_reminder,
    get_bearer_token,
    send_eskiz_sms,
)


class Command(BaseCommand):
    help = "Faqat to'lovli o'quvchilarga (student_type='paid') to'lov eslatmasi SMS yuborish (Mahalla o'quvchilari chiqarib tashlanadi)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--month',
            type=str,
            default=timezone.now().strftime('%Y-%m'),
            help="To'lov oyi formati: YYYY-MM (standart: joriy oy)",
        )
        parser.add_argument(
            '--group',
            type=int,
            default=None,
            help="Faqat ma'lum guruh o'quvchilariga yuborish (Guruh ID raqami)",
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Haqiqiy SMS yubormasdan simulyatsiya rejimida ko'rsatish",
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help="Shu oyda allaqachon eslatma olgan o'quvchilarga ham qayta yuborish",
        )
        parser.add_argument(
            '--custom-message',
            type=str,
            default=None,
            help="Maxsus xabar matni (standart matn o'rniga)",
        )
        parser.add_argument(
            '--test',
            action='store_true',
            help="TEST_SMS_PHONE raqamiga sinov eslatmasi (to'lov havolasi bilan) yuborish",
        )
        parser.add_argument(
            '--check-token',
            action='store_true',
            help="Eskiz Bearer tokeni holatini tekshirish",
        )
        parser.add_argument(
            '--test-phone',
            type=str,
            default=None,
            help="Sinov tariqasida bitta raqamga SMS yuborish",
        )
        parser.add_argument(
            '--test-message',
            type=str,
            default="Bu Eskiz dan test",
            help="--test-phone uchun sinov matni",
        )

    def handle(self, *args, **options):
        if options.get('check_token'):
            token = get_bearer_token()
            if token:
                self.stdout.write(self.style.SUCCESS("[OK] Eskiz tokeni mavjud va yaroqli."))
            else:
                self.stdout.write(self.style.ERROR("[XATO] Eskiz tokeni olinmadi. ESKIZ_EMAIL va ESKIZ_PASSWORD ni tekshiring."))
            return

        if options.get('test'):
            self.stdout.write("TEST_SMS_PHONE raqamiga sinov to'lov eslatmasi yuborilmoqda...")
            res = send_test_payment_reminder(month_str=options.get('month'))
            if res['success']:
                self.stdout.write(self.style.SUCCESS(
                    f"[{res['status'].upper()}] Sinov SMS yuborildi!\n"
                    f"Raqam: {res['test_phone']}\n"
                    f"O'quvchi: {res['student_name']}\n"
                    f"To'lov havolasi: {res['payment_url']}\n"
                    f"Xabar: {res['message']}"
                ))
            else:
                self.stdout.write(self.style.ERROR(f"[XATO] {res.get('error')}"))
            return

        if options.get('test_phone'):
            phone = options['test_phone']
            msg = options['test_message']
            self.stdout.write(f"Sinov SMS yuborilmoqda: {phone}...")
            res = send_eskiz_sms(phone, msg)
            if res['success']:
                self.stdout.write(self.style.SUCCESS(f"[{res['status'].upper()}] Muvaffaqiyatli yuborildi. ID: {res['message_id']}"))
            else:
                self.stdout.write(self.style.ERROR(f"[XATO] {res['error']}"))
            return

        month_str = options['month']
        group_id = options.get('group')
        dry_run = options.get('dry_run', False)
        force = options.get('force', False)
        custom_message = options.get('custom_message')

        self.stdout.write(self.style.MIGRATE_HEADING(
            "=== Humo Ta'lim: To'lovli O'quvchilarga SMS Eslatmalari ==="
        ))
        self.stdout.write(f"Oy: {month_str}")
        if group_id:
            self.stdout.write(f"Guruh filtri: ID {group_id}")
        if dry_run:
            self.stdout.write(self.style.WARNING("REJIM: --dry-run (Simulyatsiya, SMS yuborilmaydi)"))
        if force:
            self.stdout.write(self.style.WARNING("PARAMETR: --force (Avval yuborilganlarga ham qayta yuboriladi)"))

        self.stdout.write("To'lovli o'quvchilar ro'yxati olinmoqda (Mahalla o'quvchilari chiqarib tashlanadi)...")

        summary = send_payment_reminders_to_paid_students(
            month_str=month_str,
            group_id=group_id,
            dry_run=dry_run,
            force=force,
            custom_message=custom_message,
        )

        total = summary['total_paid_students']
        if total == 0:
            self.stdout.write(self.style.WARNING("[OGOHLANTIRISH] To'lovli faol o'quvchilar topilmadi."))
            return

        self.stdout.write(f"Jami to'lovli o'quvchilar: {total} ta")
        self.stdout.write("-" * 65)

        for item in summary['details']:
            name = item['name']
            grp = item['group']
            ph = item['phone']
            st = item['status']

            if dry_run:
                msg_prev = item.get('message', '')[:60] + "..."
                self.stdout.write(f" - [DRY-RUN] {name} ({grp}) | Tel: {ph} | Matn: {msg_prev}")
            elif st in ('sent', 'mock'):
                tag = "[MOCK]" if st == 'mock' else "[SENT]"
                self.stdout.write(self.style.SUCCESS(f" - {tag} {name} ({grp}) | Tel: {ph} -> Yuborildi"))
            elif st == 'skipped':
                self.stdout.write(self.style.WARNING(f" - [SKIPPED] {name} ({grp}) -> Avval eslatma yuborilgan"))
            elif st == 'skipped_mahalla':
                self.stdout.write(self.style.NOTICE(f" - [MAHALLA SKIP] {name} ({grp}) -> Mahalla o'quvchisi (SMS yuborilmaydi)"))
            else:
                err = item.get('error', "Xatolik")
                self.stdout.write(self.style.ERROR(f" - [FAILED] {name} ({grp}) | Tel: {ph} -> Xatolik: {err}"))

        self.stdout.write("-" * 65)
        self.stdout.write(self.style.SUCCESS(
            f"[YAKUNLANDI] Jami: {total} ta | Yuborildi: {summary['sent']} ta | "
            f"O'tkazib yuborildi: {summary['skipped']} ta | Xatolik: {summary['failed']} ta."
        ))

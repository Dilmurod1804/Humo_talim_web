"""
Django Management Command: send_tuition_reminders
Har oy boshida yoki belgilangan muddatda o'quv markazining to'lov qilmagan o'quvchilari ota-onalariga
SMS orqali xushmuomala to'lov eslatmasini yuboradi.

Foydalanish namunalari:
    python manage.py send_tuition_reminders
    python manage.py send_tuition_reminders --month 2026-11
    python manage.py send_tuition_reminders --group 4
    python manage.py send_tuition_reminders --dry-run
    python manage.py send_tuition_reminders --force
    python manage.py send_tuition_reminders --check-token
    python manage.py send_tuition_reminders --test-phone 901234567
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.sms_service import send_tuition_reminders_batch, get_month_name_uz


class Command(BaseCommand):
    help = "To'lov qilmagan (qarzdor) o'quvchilarning ota-onalariga oylik to'lov eslatmasi SMS xabarini yuborish."

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
            help="Faqat ma'lum bir guruh o'quvchilariga yuborish (Guruh ID raqami)",
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Haqiqiy SMS yubormasdan, kimlarga yuborilishini va xabar matnini konsolda ko'rsatish",
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help="Ushbu oyda allaqachon eslatma olgan o'quvchilarga ham qayta yuborish",
        )
        parser.add_argument(
            '--check-token',
            action='store_true',
            help="SMS yubormasdan Eskiz tokenini tekshirish (muddati va akkaunt holati)",
        )
        parser.add_argument(
            '--test-phone',
            type=str,
            default=None,
            help="Faqat bitta raqamga sinov SMS yuborish (masalan: 901234567)",
        )
        parser.add_argument(
            '--test-message',
            type=str,
            default="Bu Eskiz dan test",
            help="--test-phone uchun matn (Eskiz test rejimida faqat 'Bu Eskiz dan test' ruxsat etiladi)",
        )

    def _check_token(self):
        from core.sms_service import check_eskiz_token

        res = check_eskiz_token()
        if res['expires_at']:
            self.stdout.write(f"Token amal qilish muddati: {res['expires_at']:%Y-%m-%d %H:%M}")
        if res['ok']:
            data = res['data'].get('data', res['data'])
            self.stdout.write(self.style.SUCCESS("[OK] Eskiz tokeni yaroqli."))
            for key in ('name', 'email', 'role', 'status', 'balance'):
                if isinstance(data, dict) and key in data:
                    self.stdout.write(f"  {key}: {data[key]}")
        else:
            self.stdout.write(self.style.ERROR(
                f"[XATO] Token tekshiruvi muvaffaqiyatsiz (HTTP {res['status_code']}): {res['error']}"
            ))

    def _send_test(self, phone, text):
        from django.conf import settings
        from core.sms_service import send_sms

        if not getattr(settings, 'SMS_ENABLED', False):
            self.stdout.write(self.style.WARNING(
                "Diqqat: SMS_ENABLED=False — xabar haqiqatda yuborilmaydi (simulyatsiya)."
            ))
        res = send_sms(phone, text)
        if res['success']:
            self.stdout.write(self.style.SUCCESS(
                f"[{res['status'].upper()}] {phone} raqamiga yuborildi. ID: {res['message_id']}"
            ))
        else:
            self.stdout.write(self.style.ERROR(f"[FAILED] {phone}: {res['error']}"))

    def handle(self, *args, **options):
        if options.get('check_token'):
            self._check_token()
            return
        if options.get('test_phone'):
            self._send_test(options['test_phone'], options['test_message'])
            return

        month_str = options['month']
        group_id = options.get('group')
        dry_run = options.get('dry_run', False)
        force = options.get('force', False)

        try:
            y, m = map(int, month_str.split('-'))
            month_label = f"{get_month_name_uz(m)} ({month_str})"
        except Exception:
            month_label = month_str

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"=== Humo Ta'lim: To'lov Eslatmalari (SMS) ==="
        ))
        self.stdout.write(f"Ko'rilayotgan oy: {month_label}")
        if group_id:
            self.stdout.write(f"Filtr: Faqat {group_id}-ID guruh o'quvchilari")
        if dry_run:
            self.stdout.write(self.style.WARNING("REJIM: --dry-run (Simulyatsiya, haqiqiy SMS yuborilmaydi)"))
        if force:
            self.stdout.write(self.style.WARNING("PARAMETR: --force (Avval yuborilganlarga ham qayta yuboriladi)"))

        self.stdout.write("Qarzdor o'quvchilar aniqlanmoqda...")

        summary = send_tuition_reminders_batch(
            month_str=month_str,
            group_id=group_id,
            sent_by=None,
            force=force,
            dry_run=dry_run,
        )

        total = summary['total_unpaid']
        if total == 0:
            self.stdout.write(self.style.SUCCESS(
                f"[OK] {month_label} oyi uchun to'lov qilmagan qarzdor o'quvchilar topilmadi (Barcha to'lovlar qilingan)."
            ))
            return

        self.stdout.write(f"Jami to'lov qilmagan o'quvchilar soni: {total} ta")
        self.stdout.write("-" * 65)

        for item in summary['details']:
            name = item['name']
            grp = item['group']
            ph = item['phone']
            st = item['status']

            if dry_run:
                msg_preview = item.get('message', '')[:60] + "..."
                self.stdout.write(
                    f" - [DRY-RUN] {name} ({grp}) | Tel: {ph} | Matn: {msg_preview}"
                )
            elif st in ('sent', 'mock'):
                tag = "[MOCK]" if st == 'mock' else "[SENT]"
                self.stdout.write(self.style.SUCCESS(
                    f" - {tag} {name} ({grp}) | Tel: {ph} -> Muvaffaqiyatli"
                ))
            elif st == 'skipped':
                self.stdout.write(self.style.WARNING(
                    f" - [SKIPPED] {name} ({grp}) -> Avval eslatma yuborilgan"
                ))
            else:
                err = item.get('error', "Noma'lum xatolik")
                self.stdout.write(self.style.ERROR(
                    f" - [FAILED] {name} ({grp}) | Tel: {ph} -> Xatolik: {err}"
                ))

        self.stdout.write("-" * 65)
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f"[TAYYOR] {total} ta o'quvchi uchun SMS xabarlari shakllantirildi. Haqiqiy yuborish uchun --dry-run siz bajaring."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"[YAKUNLANDI] Jami qarzdorlar: {total} ta | Yuborildi: {summary['sent']} ta | "
                f"O'tkazib yuborildi: {summary['skipped']} ta | Xatolik: {summary['failed']} ta."
            ))


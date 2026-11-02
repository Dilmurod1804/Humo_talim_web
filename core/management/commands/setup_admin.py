from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

User = get_user_model()


class Command(BaseCommand):
    help = "Admin akkauntini (SSs12345) yaratish yoki parolini yangilash"

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            type=str,
            default='SSs12345',
            help='Admin username (standart: SSs12345)'
        )
        parser.add_argument(
            '--password',
            type=str,
            default='70s097oo',
            help='Admin password (standart: 70s097oo)'
        )

    def handle(self, *args, **options):
        username = options['username']
        password = options['password']

        user = User.objects.filter(username=username).first()
        if user:
            user.set_password(password)
            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            if hasattr(user, 'role'):
                user.role = 'admin'
            user.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Mavjud admin akkaunti yangilandi: login='{username}', parol muvaffaqiyatli o'rnatildi."
                )
            )
        else:
            user = User.objects.create_superuser(
                username=username,
                password=password,
                email='',
            )
            if hasattr(user, 'role'):
                user.role = 'admin'
                user.save(update_fields=['role'])
            self.stdout.write(
                self.style.SUCCESS(
                    f"Yangi admin akkaunti muvaffaqiyatli yaratildi: login='{username}', parol o'rnatildi, is_staff=True, is_superuser=True."
                )
            )

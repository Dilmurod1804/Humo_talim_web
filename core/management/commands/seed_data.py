import random
from datetime import time
from django.core.management.base import BaseCommand
from core.models import CustomUser, Teacher, Group, TimeSlot, Student

class Command(BaseCommand):
    help = 'Boshlang\'ich test ma\'lumotlarini kiritish'

    def handle(self, *args, **kwargs):
        self.stdout.write("Test ma'lumotlari kiritilmoqda...")

        # 1. Create admin if not exists
        if not CustomUser.objects.filter(username='admin').exists():
            admin = CustomUser.objects.create_superuser('admin', 'admin@example.com', 'admin123')
            admin.role = 'admin'
            admin.save()
            self.stdout.write(self.style.SUCCESS("Admin yaratildi (login: admin, parol: admin123)"))

        # 2. Create Teachers
        teachers_data = [
            ("Aziz", "Rahimov", "+998901234567"),
            ("Malika", "Tursunova", "+998931234567"),
        ]
        
        for idx, t in enumerate(teachers_data):
            login_str = f"teacher{idx+1}"
            pwd_str = "123456"
            if not CustomUser.objects.filter(username=login_str).exists():
                user = CustomUser.objects.create_user(username=login_str, password=pwd_str, role='teacher')
                teacher = Teacher.objects.create(
                    user=user,
                    first_name=t[0],
                    last_name=t[1],
                    phone=t[2],
                    generated_login=login_str,
                    generated_password=pwd_str
                )
                self.stdout.write(self.style.SUCCESS(f"O'qituvchi yaratildi: {teacher}"))
                
                # Create Group
                group = Group.objects.create(name=f"{t[0]}ning Ingliz tili guruhi", teacher=teacher)
                
                # Create TimeSlots
                ts1 = TimeSlot.objects.create(group=group, start_time=time(14, 0), end_time=time(16, 0))
                ts2 = TimeSlot.objects.create(group=group, start_time=time(16, 0), end_time=time(18, 0))
                
                # Create Students
                for i in range(3):
                    Student.objects.create(
                        first_name=f"O'quvchi_{i+1}",
                        last_name=t[0][:3],
                        age=random.randint(10, 18),
                        phone_1=f"+99890000000{i}",
                        time_slot=ts1
                    )
                for i in range(2):
                    Student.objects.create(
                        first_name=f"Talaba_{i+1}",
                        last_name=t[1][:3],
                        age=random.randint(15, 25),
                        phone_1=f"+99891000000{i}",
                        time_slot=ts2
                    )

        self.stdout.write(self.style.SUCCESS("Barcha test ma'lumotlari muvaffaqiyatli kiritildi!"))

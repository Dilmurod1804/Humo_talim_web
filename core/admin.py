from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser, Teacher, Group, TimeSlot, Student, Attendance, DeletedStudent

admin.site.register(CustomUser, UserAdmin)
admin.site.register(Teacher)
admin.site.register(Group)
admin.site.register(TimeSlot)
admin.site.register(Student)
admin.site.register(Attendance)
admin.site.register(DeletedStudent)

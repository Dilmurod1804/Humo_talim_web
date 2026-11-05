"""
HUMO O'quv Markazi — Django Signals
Avtomatik Face ID embeddinglarini yaratish (Teacher & Manager photo yuklanganda).
"""
import os
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Teacher, Manager


@receiver(post_save, sender=Teacher)
def teacher_photo_face_embedding_sync(sender, instance, **kwargs):
    """O'qituvchiga rasm yuklanganda yoki o'zgartirilganda Face ID 128-D vektorini avtomatik hisoblash."""
    if kwargs.get('update_fields') and 'face_embedding' in kwargs['update_fields']:
        return

    if instance.photo:
        try:
            if hasattr(instance.photo, 'path') and os.path.exists(instance.photo.path):
                # Faqat embedding yo'q bo'lsa yoki yangilanish talab qilinsa
                from .face_service import sync_staff_face_embedding
                sync_staff_face_embedding(instance)
        except Exception as e:
            print(f"Teacher face embedding sync error: {e}")


@receiver(post_save, sender=Manager)
def manager_photo_face_embedding_sync(sender, instance, **kwargs):
    """Menejerga rasm yuklanganda yoki o'zgartirilganda Face ID 128-D vektorini avtomatik hisoblash."""
    if kwargs.get('update_fields') and 'face_embedding' in kwargs['update_fields']:
        return

    if instance.photo:
        try:
            if hasattr(instance.photo, 'path') and os.path.exists(instance.photo.path):
                from .face_service import sync_staff_face_embedding
                sync_staff_face_embedding(instance)
        except Exception as e:
            print(f"Manager face embedding sync error: {e}")

import uuid
from django.db import migrations, models


def gen_uuid(apps, schema_editor):
    Student = apps.get_model('core', 'Student')
    seen = set()
    for s in Student.objects.all():
        new_u = uuid.uuid4()
        while new_u in seen:
            new_u = uuid.uuid4()
        seen.add(new_u)
        s.uuid = new_u
        s.save(update_fields=['uuid'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_student_student_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='student',
            name='uuid',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(gen_uuid, reverse_code=migrations.RunPython.noop),
        migrations.AlterField(
            model_name='student',
            name='uuid',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]

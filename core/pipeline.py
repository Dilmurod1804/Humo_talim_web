def set_admin_role(strategy, details, user=None, *args, **kwargs):
    if user:
        if user.role != 'admin':
            user.role = 'admin'
            user.is_superuser = True
            user.is_staff = True
            user.save()
    return {'user': user}

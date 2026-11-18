"""Server-derived hints; mutation endpoints always recheck current records."""
from core.access import is_admin, is_backoffice
from store.models import Store


def bulk_actions_for(resource, row, actor):
    if not is_backoffice(actor) or actor.must_change_password:
        return []
    if resource == 'stores':
        return ['activate', 'deactivate'] if is_admin(actor) and not row.is_official and row.status in {Store.STATUS_ACTIVE, Store.STATUS_BLOCKED} else []
    if row.pk == actor.pk or row.is_superuser or row.is_business_admin:
        return []
    if row.is_staff and not is_admin(actor):
        return []
    return ['activate', 'deactivate'] if not row.is_staff or row.staff_email_verified else ['deactivate']

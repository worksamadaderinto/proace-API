from decimal import Decimal
from django.db import models
from utils.mixins import DatesMixin
from taggit.managers import TaggableManager
from django.conf import settings

# Create your models here.

class Post(DatesMixin):
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=300)
    tags = TaggableManager()

    
    
class Content(DatesMixin):
    blog_id = models.ForeignKey('Post', on_delete=models.CASCADE)
    content =  models.TextField()   

  

class Comment(DatesMixin):
    body = models.CharField(max_length=300,blank=False,null=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    post = models.ForeignKey('Post', on_delete=models.CASCADE)


class PlatformCommissionWithdrawal(DatesMixin):
    STATUS_PENDING = 'pending'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = ((STATUS_PENDING, 'pending'), (STATUS_COMPLETED, 'completed'), (STATUS_FAILED, 'failed'))
    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        STATUS_PENDING: {STATUS_COMPLETED, STATUS_FAILED},
        STATUS_COMPLETED: set(),
        STATUS_FAILED: set(),
    }
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    payout_method = models.CharField(max_length=50)
    account_details = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='commission_withdrawals')
    reference = models.CharField(max_length=40, unique=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='processed_commission_withdrawals',
    )
    processing_note = models.TextField(blank=True, default='')

    def save(self, *args, **kwargs) -> None:
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            fields = kwargs.get("update_fields")
            if (fields is None or "status" in fields) and previous and previous != self.status:
                allowed = self.ALLOWED_TRANSITIONS.get(previous, set())
                if self.status not in allowed:
                    from rest_framework.exceptions import ValidationError
                    raise ValidationError(
                        f"Cannot transition platform commission withdrawal from terminal or invalid state '{previous}' to '{self.status}'."
                    )
        super().save(*args, **kwargs)


class TreasuryReportSummary(DatesMixin):
    """Reconciled periodic summary of settled historical ledger entries.

    Dashboard and reporting display can combine this reconciled baseline with
    the recent active tail, bounding cold-scan latency as history grows.
    Financial withdrawal authorization continues using live authoritative scans.
    """
    reconciled_through = models.DateTimeField(db_index=True)
    sales_count = models.PositiveIntegerField(default=0)
    cleared_marketplace_commission = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    cleared_official_revenue = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    cleared_escrow = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    pending_marketplace_commission = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    pending_official_revenue = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    pending_escrow = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    is_valid = models.BooleanField(default=True, db_index=True)

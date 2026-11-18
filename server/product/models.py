from decimal import Decimal
import uuid
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator


# Create your models here.
from store.models import Store
from utils.mixins import DatesMixin

from taggit.managers import TaggableManager


class Product(DatesMixin):
    CATEGORIES_CHOICE = (
        ('fishing', 'fishing'),
        ('sports', 'sports'),
        ('electronics', 'electronics'),
        ('phones', 'phones'),
        ('games', 'games'),
        ('tablets', 'tablets'),
        ('outwear', 'outwear'),
        ('pets', 'pets'),
        ('toys', 'toys'),
        ('computing', 'computing'),
        ('lingerie', 'lingerie'),
        ('books', 'books'),
        ('beverages', 'beverages'),
        ('kids', 'Kids & baby'),
        ('home', 'Home & living'),
        ('kitchen', 'Kitchen & dining'),
        ('beauty', 'Beauty & personal care'),
        ('health', 'Health & wellness'),
        ('garden', 'Garden & tools'),
        ('automotive', 'Automotive'),
        ('jewelry', 'Jewelry & watches'),
        ('software', 'Software & apps'),
        ('courses', 'Courses & learning'),
        ('templates', 'Templates & planners'),
        ('design_assets', 'Design assets'),
        ('photo_video', 'Photos & video'),
        ('music_audio', 'Music & audio'),
        ('business', 'Business resources'),
        ('digital_tools', 'Digital tools & presets'),
    )

    LABEL_CHOICE = (
        ('new', 'new'),
        ('', 'none'),
        ('bestseller', 'bestseller'),
        ('sold out', 'sold out')
    )

    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    title = models.CharField(max_length=225, blank=False, null=False)
    description = models.TextField(null=False, blank=False)
    image_url = models.URLField(blank=True, default='', max_length=1000)
    brand = models.CharField(max_length=80, blank=True, default='')
    price = models.DecimalField(
        max_digits=15, decimal_places=2, blank=False, null=False
    )
    discount = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(60)]
    )
    base_discount = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(60)],
        help_text='Regular discount percentage before or after temporary flash promotions.',
    )
    available = models.IntegerField(
        null=False, blank=False, validators=[MinValueValidator(0)]
    )
    category = models.CharField(choices=CATEGORIES_CHOICE, max_length=15)
    label = models.CharField(choices=LABEL_CHOICE, max_length=50, default='new')
    visibility = models.BooleanField(default=True)
    is_blocked = models.BooleanField(default=False, db_index=True)
    blocked_reason = models.TextField(blank=True, default='')
    blocked_at = models.DateTimeField(null=True, blank=True)
    blocked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='blocked_products',
    )
    average_rating = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=0.00,
        validators=[MinValueValidator(0), MaxValueValidator(5)]
    )
    tags = TaggableManager()
    sales = models.IntegerField(validators=[MinValueValidator(0)], default=0)
    sponsored = models.BooleanField(default=False)
    weight = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal('1.00'),
        help_text='Shipping weight in pounds (lbs).',
    )
    length = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.01'))], help_text='Package length in inches.')
    width = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.01'))], help_text='Package width in inches.')
    height = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal('0.01'))], help_text='Package height in inches.')
    is_digital = models.BooleanField(
        default=False,
        help_text='Digital products are delivered online and may be sold worldwide.',
    )
    informational_video = models.FileField(upload_to="images/product-videos/", max_length=255, blank=True, null=True)

    digital_file_url = models.CharField(
        blank=True,
        default='',
        max_length=1000,
        help_text='Private storage key exposed through authenticated, expiring download links.',
    )
    digital_preview_watermark = models.BooleanField(
        default=False,
        help_text='Overlay buyer-specific watermark on in-platform previews of digital images.',
    )
    PREVIEW_STATUS_NONE = 'none'
    PREVIEW_STATUS_PENDING = 'pending'
    PREVIEW_STATUS_PROCESSING = 'processing'
    PREVIEW_STATUS_READY = 'ready'
    PREVIEW_STATUS_FAILED = 'failed'
    PREVIEW_STATUS_CHOICES = (
        (PREVIEW_STATUS_NONE, 'None'),
        (PREVIEW_STATUS_PENDING, 'Pending'),
        (PREVIEW_STATUS_PROCESSING, 'Processing'),
        (PREVIEW_STATUS_READY, 'Ready'),
        (PREVIEW_STATUS_FAILED, 'Failed'),
    )
    ALLOWED_PREVIEW_TRANSITIONS: dict[str, set[str]] = {
        PREVIEW_STATUS_NONE: {PREVIEW_STATUS_PENDING},
        PREVIEW_STATUS_PENDING: {PREVIEW_STATUS_PROCESSING, PREVIEW_STATUS_NONE},
        PREVIEW_STATUS_PROCESSING: {PREVIEW_STATUS_READY, PREVIEW_STATUS_FAILED, PREVIEW_STATUS_PENDING, PREVIEW_STATUS_NONE},
        PREVIEW_STATUS_READY: {PREVIEW_STATUS_PENDING, PREVIEW_STATUS_NONE},
        PREVIEW_STATUS_FAILED: {PREVIEW_STATUS_PENDING, PREVIEW_STATUS_NONE},
    }
    preview_status = models.CharField(
        max_length=16,
        choices=PREVIEW_STATUS_CHOICES,
        default=PREVIEW_STATUS_NONE,
        db_index=True,
        help_text='Processing lifecycle state for protected watermarked digital previews.',
    )
    preview_error = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Failure reason if background preview generation could not complete.',
    )
    flash_sale_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Optional expiration timestamp for limited-time flash deals.',
    )
    drop_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='Release timestamp for scheduled product drops. Visible in catalog, but cart addition and purchasing are locked until this time.',
    )
    PRODUCT_TYPE_STANDARD = 'standard'
    PRODUCT_TYPE_AFFILIATE_CLICKBANK = 'affiliate_clickbank'
    PRODUCT_TYPE_CHOICE = (
        (PRODUCT_TYPE_STANDARD, 'Standard ProAce Product'),
        (PRODUCT_TYPE_AFFILIATE_CLICKBANK, 'ClickBank Affiliate Offer'),
    )

    product_type = models.CharField(
        max_length=30,
        choices=PRODUCT_TYPE_CHOICE,
        default='standard',
        help_text='Indicates whether this item is sold via ProAce cart or an external ClickBank partner.',
    )
    affiliate_hoplink = models.URLField(
        blank=True,
        default='',
        max_length=1000,
        help_text='Vendor HopLink URL for external ClickBank affiliate offers.',
    )
    affiliate_vendor_nickname = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='ClickBank vendor account nickname.',
    )
    affiliate_button_text = models.CharField(
        max_length=60,
        blank=True,
        default='Buy on Official Site',
        help_text='Custom call-to-action button label for external affiliate landing.',
    )
    variants = models.JSONField(
        default=list,
        blank=True,
        help_text='List of product variant options (e.g. size, color, stock, price delta).',
    )

    class Meta:
        indexes = [
            models.Index(fields=['-created', '-id'], name='catalog_created_id_idx'),
            models.Index(fields=['category', '-created', '-id'], name='catalog_category_page_idx'),
        ]

    @property
    def is_flash_sale_active(self) -> bool:
        if not self.flash_sale_end:
            return False
        from django.utils import timezone
        return self.flash_sale_end > timezone.now()

    @property
    def effective_discount(self) -> int:
        if self.flash_sale_end:
            from django.utils import timezone
            if self.flash_sale_end <= timezone.now():
                return self.base_discount
        return self.discount

    @property
    def sale_price(self) -> str:
        if self.product_type == self.PRODUCT_TYPE_AFFILIATE_CLICKBANK:
            return str(self.price)
        from decimal import Decimal, ROUND_HALF_UP
        discounted = self.price * (100 - self.effective_discount) / Decimal('100')
        return str(discounted.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

    @property
    def is_upcoming_drop(self) -> bool:
        if not self.drop_at:
            return False
        from django.utils import timezone
        return self.drop_at > timezone.now()

    def set_availability(self, quantity_bought: int) -> None:
        if not self.is_digital:
            self.available -= quantity_bought

    def set_sponsorship(self, switch_to: bool) -> None:
        self.sponsored = switch_to

    def clean(self) -> None:
        super().clean()
        if self.flash_sale_end:
            from django.utils import timezone
            if self.flash_sale_end > timezone.now() and self.discount <= self.base_discount:
                from django.core.exceptions import ValidationError
                raise ValidationError({
                    'discount': f'Flash deal discount ({self.discount}%) must be greater than regular discount ({self.base_discount}%).'
                })

    def save(self, *args, **kwargs) -> None:
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values_list("preview_status", flat=True).first()
            fields = kwargs.get("update_fields")
            if (fields is None or "preview_status" in fields) and previous and previous != self.preview_status:
                allowed = self.ALLOWED_PREVIEW_TRANSITIONS.get(previous, set())
                if self.preview_status not in allowed:
                    from rest_framework.exceptions import ValidationError
                    raise ValidationError(
                        f"Cannot transition product preview status from '{previous}' to '{self.preview_status}'."
                    )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_blocked and not kwargs.pop('force_moderation_override', False):
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'detail': 'Blocked products cannot be deleted while under platform moderation.'})
        return super().delete(*args, **kwargs)



class ProductImg(DatesMixin):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name='images'
    )
    image = models.ImageField(upload_to='images', default='', null=True, blank=True)


class Specification(DatesMixin):
    product = models.OneToOneField(Product, on_delete=models.CASCADE)
    serial = models.CharField(max_length=25, unique=True)
    attributes = models.CharField(max_length=250)
    height = models.DecimalField(max_digits=4, decimal_places=2)
    width = models.DecimalField(max_digits=4, decimal_places=2)
    breadth = models.DecimalField(max_digits=4, decimal_places=2)
    weight = models.DecimalField(decimal_places=2, max_digits=4)
    color = models.CharField(max_length=25)


class ProductModerationEvent(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='moderation_events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=10, choices=[('block', 'Block'), ('unblock', 'Unblock'), ('restore', 'Restore')])
    reason = models.TextField()
    created = models.DateTimeField(auto_now_add=True)


class SearchIndexJob(models.Model):
    """Durable search intent; targets deliberately survive product deletion."""

    INDEX_PRODUCT = "index_product"
    INDEX_STORE = "index_store"
    DELETE_PRODUCT = "delete_product"
    SYNC_NOTIFICATION = "sync_notification"

    action = models.CharField(max_length=20, choices=[
        (INDEX_PRODUCT, "Index product"),
        (INDEX_STORE, "Index store products"),
        (DELETE_PRODUCT, "Delete product"),
        (SYNC_NOTIFICATION, "Synchronize notification"),
    ])
    target_id = models.PositiveBigIntegerField()
    recipient_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    revision = models.UUIDField(default=uuid.uuid4, editable=False)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["action", "target_id"], name="search_job_action_target_unique"
            ),
        ]


class SearchIndexExecutionLock(models.Model):
    """Worker-only serialization; never touched by request intent writes."""
    action = models.CharField(max_length=20)
    target_id = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["action", "target_id"], name="search_execution_target_unique",
        )]

from rest_framework import serializers

from notification.models import Notification


class GenericNotificationRelatedField(serializers.RelatedField):
    def to_representation(self, value):
        if value is None:
            return None
        return {
            "id": getattr(value, "pk", None),
            "type": value.__class__.__name__,
            "label": str(value),
        }


class NotificationSerializer(serializers.ModelSerializer):
    data = serializers.SerializerMethodField()
    actor = GenericNotificationRelatedField(read_only=True)
    target = GenericNotificationRelatedField(read_only=True)
    action_object = GenericNotificationRelatedField(read_only=True)
    recipient_email = serializers.EmailField(source="recipient.email", read_only=True)
    category = serializers.SerializerMethodField()
    store = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "level",
            "unread",
            "recipient_email",
            "actor",
            "verb",
            "description",
            "target",
            "action_object",
            "public",
            "deleted",
            "emailed",
            "data",
            "timestamp",
            "read_at",
            "updated_at",
            "category",
            "store",
        ]
        read_only_fields = fields

    def get_data(self, obj: Notification) -> dict[str, object]:
        from notification.policy import notification_payload

        return notification_payload(obj)

    def get_category(self, obj: Notification) -> str:
        return obj.category

    def get_store(self, obj: Notification) -> dict[str, object] | None:
        if hasattr(obj, '_prefetched_objects_cache') and 'stores' in obj._prefetched_objects_cache:
            stores = sorted(obj.stores.all(), key=lambda s: s.pk)
            store = stores[0] if stores else None
        else:
            stores_map = self.context.setdefault('notification_stores', {})
            if not stores_map and isinstance(self.parent, serializers.ListSerializer):
                from notification.models import Notification
                through_model = Notification.stores.through
                notification_pks = [n.pk for n in self.parent.instance if hasattr(n, 'pk')]
                store_links = through_model.objects.filter(
                    notification_id__in=notification_pks
                ).select_related('store').order_by('notification_id', 'store_id')
                for link in store_links:
                    if link.notification_id not in stores_map:
                        stores_map[link.notification_id] = link.store
            store = stores_map.get(obj.pk) if stores_map else obj.stores.order_by("pk").first()

        if store is None:
            return None
        return {"id": store.pk, "name": store.name, "username": store.username}


class NotificationCountSerializer(serializers.Serializer):
    unread = serializers.IntegerField()
    read = serializers.IntegerField()
    archived = serializers.IntegerField()
    total = serializers.IntegerField()


class PushDeviceTokenSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=4096, trim_whitespace=True)
    platform = serializers.ChoiceField(choices=['web', 'ios', 'android'], default='web')


class PushDeviceStatusSerializer(serializers.Serializer):
    registered = serializers.BooleanField()
    has_devices = serializers.BooleanField()
    device_count = serializers.IntegerField()
    fcm_enabled = serializers.BooleanField(default=False)

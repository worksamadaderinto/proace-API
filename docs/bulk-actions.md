# Backoffice selection and tax policy

`GET /api/v1/admin/bulk-actions/` returns the authoritative `max_records` limit (`ADMIN_BULK_MAX_RECORDS`, default 100). `POST` accepts a resource (`taxes`, `coupons`, `staff`, `users`, `stores`), distinct positive `ids`, an explicit JSON `is_active` boolean and an optional store moderation reason.

The endpoint locks selected records in ID order, validates the entire selection and commits atomically. Invalid IDs, protected accounts or ineligible records reject the operation rather than silently skipping records. Repeating a satisfied assignment reports zero changed rows without repeating access notifications. Responses contain counts, not global record payloads.

Staff/store access changes require platform administrator privileges. Ordinary staff can manage customer access, coupons and tax rules under the existing backoffice policy. Operators cannot change their own or administrator accounts. Unverified staff cannot be activated. Official and pending-review stores use their dedicated workflows. Backoffice serializers provide actor-scoped `bulk_actions` hints; mutations always recheck current authorization and records.

## Global checkout tax pause

`TaxConfiguration.taxes_enabled` is independent of individual jurisdiction activation. `POST /api/v1/admin/taxes/toggle-all/` requires an explicit JSON boolean, pauses/resumes all tax providers including address-free calculations, and preserves individual rule flags and historical order snapshots. Selected tax-rule actions affect only those records. Disabling the last individual rule never infers a global pause.

Migration `0027_tax_configuration` preserves the previous effective paused mode once when legacy rules exist and all are inactive. Individual flags are preserved, so operators should review enabled rules before resuming a previously paused configuration. Empty configuration is distinct from an explicit global pause.

## Account closure

`DELETE /api/v1/me/` deactivates the account, increments credential version, blocks new purchases from owned stores and queues a recipient-specific confirmation. Order/tax/payment records and personal data remain stored. Operator accounts use protected administration. This is closure, not personal-data erasure or proof of a retention-policy/App Store compliance claim.

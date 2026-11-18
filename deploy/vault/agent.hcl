vault {
  address = "https://vault.example.invalid"
  # For a private CA, set ca_cert to the installed CA certificate file.
}
auto_auth {
  method "approle" {
    mount_path = "auth/approle"
    config = {
      role_id_file_path = "/etc/proace/vault/role-id"
      secret_id_file_path = "/run/proace-bootstrap/wrapped-secret-id"
      remove_secret_id_file_after_reading = true
      secret_id_response_wrapping_path = "auth/approle/role/proace-api/secret-id"
    }
  }
}
template_config {
  static_secret_render_interval = "1m"
  exit_on_retry_failure = true
}
template {
  source = "/etc/proace/vault/templates/api.ctmpl"
  destination = "/run/proace-vault/api/runtime.json"
  perms = "0440"
  error_on_missing_key = true
}
template {
  source = "/etc/proace/vault/templates/postgres.ctmpl"
  destination = "/run/proace-vault/api/postgres-password"
  perms = "0440"
  error_on_missing_key = true
}
template {
  source = "/etc/proace/vault/templates/redis.ctmpl"
  destination = "/run/proace-vault/api/redis.conf"
  perms = "0440"
  error_on_missing_key = true
}
template {
  source = "/etc/proace/vault/templates/minio-user.ctmpl"
  destination = "/run/proace-vault/api/minio-user"
  perms = "0440"
  error_on_missing_key = true
}
template {
  source = "/etc/proace/vault/templates/minio-password.ctmpl"
  destination = "/run/proace-vault/api/minio-password"
  perms = "0440"
  error_on_missing_key = true
}

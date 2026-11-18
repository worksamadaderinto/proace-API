ui = true
api_addr = "https://vault.example.invalid"
cluster_addr = "https://vault.example.invalid:8201"
storage "raft" {
  path = "/vault/data"
  node_id = "proace-vault-1"
}
listener "tcp" {
  address = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_cert_file = "/vault/tls/fullchain.pem"
  tls_key_file = "/vault/tls/privkey.pem"
  tls_min_version = "tls12"
}

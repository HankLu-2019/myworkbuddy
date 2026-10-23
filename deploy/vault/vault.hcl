# HashiCorp Vault for MyWorkBuddy's stored secrets (Transit only). Reached on the compose default network as
# http://vault:8200, never published to the host. Use the company's Vault instead by setting MWB_VAULT_ADDR to it
# and not starting this service.
storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true # internal network only; MWB_VAULT_ADDR must be https for any other host
}

api_addr      = "http://vault:8200"
ui            = false
disable_mlock = false

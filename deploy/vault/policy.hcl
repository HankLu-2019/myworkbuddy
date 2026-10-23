# What MyWorkBuddy may do in Vault: make, unwrap and rewrap data keys under one Transit key. Nothing else: it can't
# read the key, export it, rotate it or delete it.
path "transit/datakey/plaintext/mwb" { capabilities = ["update"] }
path "transit/decrypt/mwb"           { capabilities = ["update"] }
path "transit/rewrap/mwb"            { capabilities = ["update"] }

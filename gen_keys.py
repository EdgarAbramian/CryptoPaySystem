import bip32utils, os, hashlib

# Генерируем мастер-ключ из случайного seed
seed = os.urandom(32)
print(f"Seed (сохрани!): {seed.hex()}")

master = bip32utils.BIP32Key.fromEntropy(seed)

# BIP44 path для testnet: m/44'/1'/0'
purpose  = master.ChildKey(44  + bip32utils.BIP32_HARDEN)
coin     = purpose.ChildKey(1  + bip32utils.BIP32_HARDEN)  # 1 = testnet
account  = coin.ChildKey(0     + bip32utils.BIP32_HARDEN)

print(f"\nBTC_XPUB={account.PublicKey().hex()}")
print(f"BTC_XPUB (extended)={account.ExtendedKey(private=False)}")
print(f"BTC_XPRIV (extended)={account.ExtendedKey(private=True)}")


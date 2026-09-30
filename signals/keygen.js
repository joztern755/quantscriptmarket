#!/usr/bin/env node
'use strict';
// Prints a NEW Ed25519 keypair for the signal feed. Nothing is written to disk.
//   node signals/keygen.js
// - Private key (PKCS#8 PEM): store ONLY as the terminal repo's GitHub Actions secret SIGNALS_ED25519_PRIVATE_KEY_PEM.
//   Never commit it, never paste it into chat, never put it in the marketplace backend.
// - Public key (raw 32 bytes, base64): the marketplace pins it as SIGNALS_PUBKEY_B64 (Secret Manager / env). Optionally
//   also set it as the terminal repo variable SIGNALS_ED25519_PUBLIC_KEY_B64 so emit.js refuses a mismatched secret.
// Rotation: generate a new pair, set the new secret, deploy the backend with the new public key, then remove the old secret.
const crypto = require('crypto');
const { publicKeyB64, signBytes, verifyBytes, publicKeyFromB64 } = require('./lib.js');

const { privateKey } = crypto.generateKeyPairSync('ed25519');
const pem = privateKey.export({ type: 'pkcs8', format: 'pem' });
const pub = publicKeyB64(privateKey);
// self-check before printing
const probe = Buffer.from('aijalon keygen self-check');
if (!verifyBytes(probe, signBytes(probe, privateKey), publicKeyFromB64(pub))) throw new Error('keygen self-check failed');

process.stdout.write(
  '# Ed25519 keypair for aijalon.trade signals — generated ' + new Date().toISOString() + '\n' +
  '# PRIVATE (GitHub secret SIGNALS_ED25519_PRIVATE_KEY_PEM in the terminal repo; never commit):\n' +
  pem +
  '# PUBLIC (marketplace SIGNALS_PUBKEY_B64; terminal variable SIGNALS_ED25519_PUBLIC_KEY_B64):\n' +
  pub + '\n');

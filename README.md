# Encrypted project data transfer

Contains consistent snapshots of all four project SQLite databases, all data/media files, other data files and .env. AES-256-GCM. Transfer transfer-key.hex separately; the key is NOT in Git.

Install Python 3 on the destination, then run:

```sh
python -m pip install cryptography
python restore.py --key-file /path/to/transfer-key.hex --output /path/to/new-empty-directory
```

Copy restored data/ and .env into your backend checkout. Restore refuses to overwrite existing files. This branch contains data only; current local source code and migrations are not included.

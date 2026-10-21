#!/usr/bin/env python3
# Pack Parquet files into the region layout of oasis_rdma_server, for the simulation mock.
# Usage: pack_image.py <image.bin> <file.parquet> ...
import os, struct, sys
out, files = sys.argv[1], sys.argv[2:]
names = [os.path.basename(f) for f in files]
sizes = [os.path.getsize(f) for f in files]
dir_size = 8 + sum(8 + len(n) + 16 for n in names)
offsets, cur = [], dir_size
for s in sizes:
    offsets.append(cur)
    cur += s
with open(out, "wb") as o:
    o.write(struct.pack("<Q", dir_size))
    for n, off, s in zip(names, offsets, sizes):
        b = n.encode()
        o.write(struct.pack("<Q", len(b)) + b + struct.pack("<QQ", off, s))
    for f in files:
        o.write(open(f, "rb").read())
    o.write(bytes(64))
for n, off, s in zip(names, offsets, sizes):
    print(f"{n} offset={off} size={s}")

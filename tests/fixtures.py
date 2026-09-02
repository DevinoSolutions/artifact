"""Create the fixture tree used by CI.

data/
  a.txt
  .hidden.txt          (excluded unless include-hidden-files)
  sub/b.bin            (large on Linux to exercise multipart through Cloudflare)
  sub/skip.txt
  sub/.dot/c.txt       (hidden directory)
"""
import os
import sys

root, os_name = sys.argv[1], sys.argv[2]
os.makedirs(os.path.join(root, "sub", ".dot"), exist_ok=True)
with open(os.path.join(root, "a.txt"), "w") as f:
    f.write("hello from %s\n" % os_name)
with open(os.path.join(root, ".hidden.txt"), "w") as f:
    f.write("hidden\n")
with open(os.path.join(root, "sub", "skip.txt"), "w") as f:
    f.write("skip me\n")
with open(os.path.join(root, "sub", ".dot", "c.txt"), "w") as f:
    f.write("dot dir\n")
size = 130 * 1024 * 1024 if os_name.startswith("ubuntu") else 6 * 1024 * 1024
with open(os.path.join(root, "sub", "b.bin"), "wb") as f:
    remaining = size
    while remaining > 0:
        chunk = os.urandom(min(remaining, 1 << 20))
        f.write(chunk)
        remaining -= len(chunk)
print("fixtures written to", root, "b.bin =", size, "bytes")

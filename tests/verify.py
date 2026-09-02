"""Assertions for the CI round-trip tests."""
import hashlib
import os
import sys


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def must(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("ok:", msg)


def same(a, b):
    must(os.path.isfile(b), "%s exists" % b)
    must(sha(a) == sha(b), "%s matches %s" % (b, a))


mode = sys.argv[1]
data = sys.argv[2]

if mode == "single":
    out = sys.argv[3]
    same(os.path.join(data, "a.txt"), os.path.join(out, "a.txt"))
    same(os.path.join(data, "sub", "b.bin"), os.path.join(out, "sub", "b.bin"))
    same(os.path.join(data, "sub", "skip.txt"), os.path.join(out, "sub", "skip.txt"))
    must(not os.path.exists(os.path.join(out, ".hidden.txt")), "hidden file excluded")
    must(not os.path.exists(os.path.join(out, "sub", ".dot")), "hidden dir excluded")
    must(not os.path.exists(os.path.join(out, "data")), "root is the directory itself, not its parent")

elif mode == "multi":
    merged, split, os_name = sys.argv[3], sys.argv[4], sys.argv[5]
    # merged: multi-a (a.txt, sub/b.bin, .hidden.txt) + multi-b (a.txt, sub/skip.txt excluded)
    same(os.path.join(data, "a.txt"), os.path.join(merged, "a.txt"))
    same(os.path.join(data, "sub", "b.bin"), os.path.join(merged, "sub", "b.bin"))
    same(os.path.join(data, ".hidden.txt"), os.path.join(merged, ".hidden.txt"))
    must(not os.path.exists(os.path.join(merged, "sub", "skip.txt")), "excluded pattern honoured")
    must(not os.path.exists(os.path.join(merged, "sub", ".dot")), "hidden dir excluded from glob upload")
    a = os.path.join(split, "multi-a-" + os_name)
    b = os.path.join(split, "multi-b-" + os_name)
    same(os.path.join(data, "a.txt"), os.path.join(a, "a.txt"))
    same(os.path.join(data, "sub", "b.bin"), os.path.join(a, "sub", "b.bin"))
    same(os.path.join(data, "a.txt"), os.path.join(b, "a.txt"))
    must(not os.path.exists(os.path.join(b, "sub", "b.bin")), "glob upload only has txt files")
else:
    print("unknown mode", mode)
    sys.exit(2)
print("all good")

import sys

name = sys.argv[1] if len(sys.argv) > 1 else "_it.log"
lines = open(name, encoding="utf-8", errors="replace").read().splitlines()
print("PASS:", sum(1 for l in lines if l.startswith("PASS")))
fails = [l for l in lines if l.startswith("FAIL")]
print("FAIL:", len(fails))
for l in fails:
    print("  ", l)
print("--- tail ---")
for l in lines[-4:]:
    print(l)

def build(n):
    xs = []
    for i in range(n): xs.append((i * 7) % 1000)
    return xs
total = 0
for r in range(10):
    xs = build(5000000)
    for x in xs: total += x
print(total)

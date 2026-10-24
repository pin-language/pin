def steps(n):
    s = 0
    while n != 1:
        if n % 2 == 0: n = n // 2
        else: n = 3 * n + 1
        s += 1
    return s
best = 0; arg = 0
for i in range(1, 1000000):
    s = steps(i)
    if s > best: best = s; arg = i
print(arg, best)

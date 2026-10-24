n = 20000000
composite = [False] * (n + 1)
count = 0
for i in range(2, n + 1):
    if not composite[i]:
        count += 1
        for j in range(i * i, n + 1, i):
            composite[j] = True
print(count)

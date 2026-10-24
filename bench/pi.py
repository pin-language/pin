s = 0.0
sign = 1.0
for k in range(100000000):
    s += sign / (2 * k + 1)
    sign = -sign
print(f"{4.0 * s:.14G}")

let mut k = n;
while k >= 2
    invariant k <= n, k % 2 == n % 2,
    decreases k,
{ k = k - 2; }
k == 0

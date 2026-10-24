fn build(n: i64) -> Vec<i64> {
    let mut xs = Vec::new();
    for i in 0..n { xs.push((i * 7) % 1000); }
    xs
}
fn main() {
    let mut total: i64 = 0;
    for _ in 0..10 { let xs = build(5000000); for x in &xs { total += x; } }
    println!("{}", total);
}

fn main() {
    let (mut sum, mut sign) = (0.0f64, 1.0f64);
    for k in 0..100000000i64 { sum += sign / (2 * k + 1) as f64; sign = -sign; }
    let s = format!("{:.13e}", 4.0 * sum); // 14 significant digits
    let v: f64 = s.parse().unwrap();
    println!("{}", v);
}

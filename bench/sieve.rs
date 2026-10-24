fn main() {
    let n: usize = 20000000;
    let mut composite = vec![false; n + 1];
    let mut count = 0;
    for i in 2..=n {
        if !composite[i] {
            count += 1;
            let mut j = i * i;
            while j <= n { composite[j] = true; j += i; }
        }
    }
    println!("{}", count);
}

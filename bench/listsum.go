package main
import "fmt"
func build(n int64) []int64 {
	var xs []int64
	for i := int64(0); i < n; i++ { xs = append(xs, (i*7)%1000) }
	return xs
}
func main() {
	var total int64
	for r := 0; r < 10; r++ { xs := build(5000000); for _, x := range xs { total += x } }
	fmt.Println(total)
}

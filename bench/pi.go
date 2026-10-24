package main
import ("fmt"; "strconv")
func main() {
	sum, sign := 0.0, 1.0
	for k := 0; k < 100000000; k++ { sum += sign / float64(2*k+1); sign = -sign }
	fmt.Println(strconv.FormatFloat(4.0*sum, 'G', 14, 64))
}

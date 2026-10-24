package main

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
)

type User struct {
	ID     int64   `json:"id"`
	Name   string  `json:"name"`
	Email  string  `json:"email"`
	Orders []int64 `json:"orders"`
	Total  int64   `json:"total"`
}

type Primes struct {
	N     int64 `json:"n"`
	Count int64 `json:"count"`
}

func user(id int64) []byte {
	orders := make([]int64, 0, 20)
	var total int64
	for i := int64(0); i < 20; i++ {
		amount := (id * (i + 7)) % 997
		orders = append(orders, amount)
		total += amount
	}
	s := strconv.FormatInt(id, 10)
	b, _ := json.Marshal(User{id, "user" + s, "user" + s + "@pinato.co", orders, total})
	return b
}

func primes(w http.ResponseWriter, n int64) []byte {
	if n < 2 || n > 1000000 {
		w.WriteHeader(400)
		return []byte(`{"error":"n must be between 2 and 1000000"}`)
	}
	composite := make([]bool, n+1)
	var count int64
	for i := int64(2); i <= n; i++ {
		if !composite[i] {
			count++
			for j := i * i; j <= n; j += i {
				composite[j] = true
			}
		}
	}
	b, _ := json.Marshal(Primes{n, count})
	return b
}

func atoi(s string) int64 { v, _ := strconv.ParseInt(s, 10, 64); return v }

func handle(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	p := r.URL.Path
	var body []byte
	switch {
	case p == "/hello":
		body = []byte(`{"message":"hello world"}`)
	case strings.HasPrefix(p, "/user/"):
		body = user(atoi(p[6:]))
	case strings.HasPrefix(p, "/primes/"):
		body = primes(w, atoi(p[8:]))
	default:
		w.WriteHeader(404)
		body = []byte(`{"error":"not found"}`)
	}
	w.Write(body)
}

func main() {
	http.HandleFunc("/", handle)
	http.ListenAndServe(":8082", nil)
}

// Command bootstrap is a build fixture: CI and developers build it in the pinned container
// and compare the ZIP digest with expected-SHA256SUMS to show that builds match across machines.
package main

import "fmt"

func main() {
	fmt.Println("hello from a reproducible Lambda build")
}

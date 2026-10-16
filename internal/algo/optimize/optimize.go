// Package optimize：志愿表排序与配比（约束满足 + 启发式搜索）。
//
// 设计见 docs/algorithms.md §6。纯函数、无 IO、确定性（固定随机种子或完全确定性比较）。
//
// 核心：平行志愿结果 = "第一个录取你的院校"。目标：
//
//	E[U] = Σ_k U_(k)·P_(k)·Π_{i<k}(1−P_(i)) + U_slip·Π_all(1−P_(i))
package optimize

import "errors"

// ErrNotImplemented 占位错误。
var ErrNotImplemented = errors.New("optimize: not implemented")

// Band 冲稳保档位（见 docs/algorithms.md §5）。
type Band int

const (
	BandUncertain Band = iota // 依据不足
	BandReach                 // 垫（P < 0.20）
	BandRush                  // 冲（0.20 ≤ P < 0.50）
	BandStable                // 稳（0.50 ≤ P < 0.80）
	BandSafe                  // 保（P ≥ 0.80）
)

// Scored 一个候选 + 它的概率与效用（由 prob / 上层注入）。
type Scored struct {
	ID   string
	Prob float64 // P_j
	Util float64 // U_j ∈ [0,1]
	Band Band
}

// Params 优化参数。
type Params struct {
	MaxSlots int // L：本省志愿数上限（以规则为准）

	// 硬约束：保底个数下限 B_min（默认 max(3, 20%·L)），防滑档。
	SafeMin int

	// 软约束：冲稳保配比（默认 0.3 / 0.4 / 0.3）。
	RushRatio, StableRatio, SafeRatio float64

	SlipUtil float64 // U_slip：滑档效用（取最小值）
}

// Report 优化结果的可解释信息。
type Report struct {
	SlipProb     float64      // Π(1−P)
	ExpectedUtil float64      // E[U]
	Bands        map[Band]int // 各档实际数量
}

// Wishlist 选出并排序一份志愿表。
//
// 排序固定为 U 降序（可证占优）；真正的决策是"选哪些进来"：
//  1. 贪心：按边际 E[U] 增量插入，直到 MaxSlots；
//  2. 局部搜索：两两交换 / 替换，改进 E[U] 与软约束；
//  3. 先满足硬约束（MaxSlots、SafeMin）。
func Wishlist(cands []Scored, p Params) ([]Scored, Report, error) {
	// TODO(M4): 见 docs/algorithms.md §6.4/§6.5。
	return nil, Report{}, ErrNotImplemented
}

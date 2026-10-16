// Package sim：平行志愿投档模拟（分数优先、遵循志愿）+ 蒙特卡洛校验。
//
// 设计见 docs/algorithms.md §7。确定性可复现（固定种子）。
package sim

// Result 一次模拟的结果。
type Result struct {
	Admitted bool
	HitIndex int // 命中的志愿下标；未录取为 -1
}

// Simulate 用给定校线（百分位，按志愿提交顺序）模拟一遍投档。
//
// 规则：依次扫描志愿，命中第一个满足 `userQ ≤ cutoffs[k]` 的志愿。
func Simulate(cutoffs []float64, userQ float64) Result {
	for k := range cutoffs {
		if userQ <= cutoffs[k] {
			return Result{Admitted: true, HitIndex: k}
		}
	}
	return Result{Admitted: false, HitIndex: -1}
}

// Distribution 某候选的校线分布（供蒙特卡洛采样）。
type Distribution struct {
	Mean, Std float64
}

// MonteCarlo 重复 n 次采样校线并模拟，估计滑档率与期望效用。
//
// utils 与 cutoffs 一一对应（各志愿的效用）；slipUtil 为滑档效用。
func MonteCarlo(cutoffs []Distribution, userQ float64, utils []float64, slipUtil float64, n int) (slipProb, expectedUtil float64) {
	// TODO(M4): 每次为每个候选采样一个校线 → Simulate → 统计滑档频率与效用均值。
	//           用于校验 optimize 的解析 E[U]，并给出"滑档风险"这一关键数字。
	return 0, 0
}

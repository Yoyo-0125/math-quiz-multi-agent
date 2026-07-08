SPLIT_INPUT_MESSAGE = "输入过多或题目难度过大，请拆分后再输入。"


class BudgetExceededError(RuntimeError):
    pass


def estimate_tokens(text):
    if text is None:
        return 0
    # Conservative approximation for mixed Chinese, LaTeX, and JSON prompts.
    return max(1, int(len(str(text)) * 0.65))


def _usage_total_tokens(usage):
    if not isinstance(usage, dict):
        return None

    total_tokens = usage.get("total_tokens")
    if isinstance(total_tokens, int):
        return total_tokens

    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if isinstance(prompt_tokens, int) and isinstance(completion_tokens, int):
        return prompt_tokens + completion_tokens

    return None


class TokenBudget:
    def __init__(
        self,
        total_warning=180000,
        total_stop=270000,
        single_warning=45000,
        single_stop=65000,
    ):
        self.total_warning = int(total_warning)
        self.total_stop = int(total_stop)
        self.single_warning = int(single_warning)
        self.single_stop = int(single_stop)
        self.used_tokens = 0
        self.warned_total = False

    def check_request(self, agent_name, prompt_tokens, max_output_tokens):
        estimated_total = int(prompt_tokens) + int(max_output_tokens)

        if self.single_stop > 0 and estimated_total >= self.single_stop:
            raise BudgetExceededError(
                f"{SPLIT_INPUT_MESSAGE} "
                f"{agent_name} 单次预计 token={estimated_total}，"
                f"已达到停止阈值 {self.single_stop}。"
            )

        if self.single_warning > 0 and estimated_total >= self.single_warning:
            print(
                f"Token warning: {agent_name} 单次预计 token={estimated_total}，"
                f"接近单次阈值 {self.single_warning}。"
            )

        projected_total = self.used_tokens + estimated_total
        if self.total_stop > 0 and projected_total >= self.total_stop:
            raise BudgetExceededError(
                f"{SPLIT_INPUT_MESSAGE} "
                f"累计预计 token={projected_total}，"
                f"已达到停止阈值 {self.total_stop}。"
            )

        if (
            self.total_warning > 0
            and projected_total >= self.total_warning
            and not self.warned_total
        ):
            print(
                f"Token warning: 累计预计 token={projected_total}，"
                f"接近总量阈值 {self.total_warning}。"
            )
            self.warned_total = True

        return estimated_total

    def record_usage(self, agent_name, usage, fallback_total):
        actual_total = _usage_total_tokens(usage)
        if actual_total is None:
            actual_total = int(fallback_total)

        self.used_tokens += actual_total

        if self.total_stop > 0 and self.used_tokens >= self.total_stop:
            raise BudgetExceededError(
                f"{SPLIT_INPUT_MESSAGE} "
                f"累计实际 token={self.used_tokens}，"
                f"已达到停止阈值 {self.total_stop}。"
            )

        if (
            self.total_warning > 0
            and self.used_tokens >= self.total_warning
            and not self.warned_total
        ):
            print(
                f"Token warning: 累计实际 token={self.used_tokens}，"
                f"接近总量阈值 {self.total_warning}。"
            )
            self.warned_total = True

        return actual_total

    def summary(self):
        return {
            "used_tokens": self.used_tokens,
            "total_warning": self.total_warning,
            "total_stop": self.total_stop,
            "single_warning": self.single_warning,
            "single_stop": self.single_stop,
        }

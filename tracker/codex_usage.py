"""Reconcile cumulative usage; replay this state over the prefix on every import.

Compaction/resume are NOT counter resets. Only an observed decrease resets the
baseline. Unverified last-only events are bridged out of the next cumulative
delta to avoid charging them twice. No historical content is executed.
"""
KEYS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
        'reasoning_output_tokens', 'cache_write_input_tokens')


def numbers(value):
    if not isinstance(value, dict):
        return {}
    return {k: value[k] for k in KEYS if type(value.get(k)) is int and value[k] >= 0}


class UsageCounter:
    def __init__(self):
        self.previous = {}
        self.bridge = {k: 0 for k in KEYS}
        self.compacted = False

    def consume(self, info):
        if not isinstance(info, dict):
            return None, 'missing_usage'
        total = numbers(info.get('total_token_usage'))
        last = numbers(info.get('last_token_usage'))
        if not total:
            if not last:
                return None, 'missing_usage'
            for k, v in last.items():
                self.bridge[k] += v
            return self.validated(last, 'last_only_unverified')

        if not self.previous and not any(total.values()):
            self.previous = dict(total)
            return None, 'zero_usage'
        common = set(total) & set(self.previous)
        if common and all(total[k] == self.previous[k] for k in common) and set(total) <= set(self.previous):
            return None, 'duplicate_cumulative'
        reset = any(total[k] < self.previous[k] for k in common)
        if not self.previous or reset:
            # A resumed/forked log may begin with inherited cumulative totals.
            # Prefer the current step if present; do not charge inherited history.
            usage = dict(last) if last else dict(total)
            status = 'counter_reset' if reset else ('initial_last' if last else 'initial_total')
        else:
            usage = {}
            for k, v in total.items():
                if k in self.previous:
                    usage[k] = v - self.previous[k] - self.bridge[k]
                elif k in last:
                    usage[k] = last[k]
                else:
                    # Newly available optional cumulative field has no baseline.
                    # Its lifetime value is not this step's consumption.
                    usage[k] = 0
            for k, v in last.items():
                if k not in total:
                    usage[k] = v
            if any(v < 0 for v in usage.values()):
                # A reset can be masked by an intervening last-only event.
                usage = dict(last) if last else {}
                status = 'counter_discontinuity_unverified'
            else:
                status = 'cumulative_verified' if last and all(usage.get(k) == v for k, v in last.items()) else 'cumulative_corrected'
        if reset:
            self.previous = {}
            self.bridge = {k: 0 for k in KEYS}
        for k, v in total.items():
            self.previous[k] = v
            self.bridge[k] = 0
        # Last-only optional fields also need bridging when they reappear.
        for k, v in usage.items():
            if k not in total:
                self.bridge[k] += max(0, v)
        if self.compacted:
            status += ':after_compaction'
            self.compacted = False
        return self.validated(usage, status)

    @staticmethod
    def validated(usage, status):
        if 'input_tokens' not in usage or 'output_tokens' not in usage:
            status += ':partial_fields'
        values = {k: max(0, usage.get(k, 0)) for k in KEYS}
        if values['cached_input_tokens'] > values['input_tokens'] or values['reasoning_output_tokens'] > values['output_tokens']:
            return None, 'invalid_usage_subsets'
        if not any(values.values()):
            return None, 'zero_usage'
        return values, status

#include "hw_bitmap.hpp"

#include "duckdb/common/limits.hpp"
#include "duckdb/common/types/hash.hpp"
#include "duckdb/planner/expression/bound_function_expression.hpp"
#include "duckdb/planner/expression/bound_operator_expression.hpp"
#include "duckdb/planner/filter/expression_filter.hpp"
#include "duckdb/planner/filter/table_filter_functions.hpp"
#include "hw_bloom.hpp"
#include "hw_filter.hpp"
#include "oasis/configuration.hpp"

#include <algorithm>

namespace duckdb {

// A bitmap takes at most this share of the lookup table, so that the other bitmaps of the queries fit.
static constexpr size_t BITMAP_TABLE_SHARE = 2;
// A folded bitmap of an IN list takes at most this many bits: its few values set as many bits.
static constexpr uint64_t FOLDED_IN_LIST_MAX_BITS = 4096;
// A prefix range filter that folds may go as a Bloom filter of its keys instead, if its span has at
// most this many keys: the span of the exact prefix range filters of DuckDB (SMALL_EXACT_PRF_BITS).
static constexpr uint64_t KEYS_BLOOM_MAX_SPAN = uint64_t(1) << 26;

static uint64_t BitmapBits(int64_t min, int64_t max, uint32_t shift) {
	return ((uint64_t(max) - uint64_t(min)) >> shift) + 1;
}

double HwBitmap::OnesFraction(int64_t lo, int64_t hi) const {
	D_ASSERT(!words.empty() && min <= lo && lo <= hi && hi <= max);
	// The set bits before bit b
	auto ones_until = [&](uint64_t b) {
		uint64_t ones = ones_before[b / 64];
		if (b % 64 != 0) {
			ones += __builtin_popcountll(words[b / 64] & ((uint64_t(1) << (b % 64)) - 1));
		}
		return ones;
	};
	const uint64_t first = (uint64_t(lo) - uint64_t(min)) >> shift;
	const uint64_t last = (uint64_t(hi) - uint64_t(min)) >> shift;
	return double(ones_until(last + 1) - ones_until(first)) / double(last - first + 1);
}

double HwBitmap::PassFraction(int64_t lo, int64_t hi) const {
	if (bloom) {
		const double keys = std::min(1.0, bloom_keys / (double(uint64_t(hi) - uint64_t(lo)) + 1));
		return keys + (1 - keys) * bloom_pass;
	}
	return words.empty() ? 1 : OnesFraction(lo, hi);
}

std::shared_ptr<oasis::FilterTableRegion> HwBitmap::Region(oasis::FilterTable &table) const {
	std::call_once(region_allocated, [&]() { region = table.allocate(words); });
	return region;
}

// The largest bitmap a new region can hold now, in bits.
static uint64_t BitmapBudget(oasis::FilterTable &table) {
	return std::min(table.largest_free_words(), table.words() / BITMAP_TABLE_SHARE) * 64;
}

// The words of the largest Bloom filter a new region can hold now, a power of two; 0 for none.
static uint64_t BloomWords(oasis::FilterTable &table) {
	const uint64_t max_words = BitmapBudget(table) / 64;
	return max_words == 0 ? 0 : PreviousPowerOfTwo(max_words);
}

// The smallest shift up to max_shift that fits the bitmap of [min, max] into budget bits.
static std::optional<uint32_t> FoldShift(int64_t min, int64_t max, uint64_t budget, uint32_t max_shift) {
	for (uint32_t shift = 0; shift <= max_shift; shift++) {
		if (BitmapBits(min, max, shift) <= budget) {
			return shift;
		}
	}
	return std::nullopt;
}

void SetBit(std::vector<uint64_t> &words, uint64_t bit) {
	words[bit / 64] |= uint64_t(1) << (bit % 64);
}

std::shared_ptr<const HwBitmap> MakeBitmap(int64_t min, int64_t max, uint32_t shift, bool exact,
                                           std::vector<uint64_t> words) {
	auto bitmap = std::make_shared<HwBitmap>();
	bitmap->min = min;
	bitmap->max = max;
	bitmap->shift = shift;

	const uint64_t num_bits = BitmapBits(min, max, shift);
	uint64_t ones = 0;
	bitmap->ones_before.reserve(words.size() + 1);
	for (auto word : words) {
		bitmap->ones_before.push_back(uint32_t(ones));
		ones += __builtin_popcountll(word);
	}
	bitmap->ones_before.push_back(uint32_t(ones));

	// Every bit set is the interval, also for an exact bitmap
	if (ones == num_bits || (!exact && double(ones) > FOLDED_BITMAP_MAX_ONES * double(num_bits))) {
		return bitmap;
	}
	bitmap->words = std::move(words);
	return bitmap;
}

// Calls found(i) for every key min + i, i < num_keys, of the prefix range filter.
template <class T, class F>
static void ForEachKey(const PrefixRangeFilter &prf, const LogicalType &key_type, int64_t min, uint64_t num_keys,
                       F found) {
	Vector keys(key_type, STANDARD_VECTOR_SIZE);
	auto data = FlatVector::GetDataMutable<T>(keys);
	SelectionVector sel(STANDARD_VECTOR_SIZE);
	for (uint64_t start = 0; start < num_keys; start += STANDARD_VECTOR_SIZE) {
		const idx_t count = idx_t(std::min<uint64_t>(STANDARD_VECTOR_SIZE, num_keys - start));
		for (idx_t i = 0; i < count; i++) {
			data[i] = T(uint64_t(min) + start + i);
		}
		const idx_t num_found = prf.LookupKeys(keys, sel, count);
		for (idx_t i = 0; i < num_found; i++) {
			found(start + sel.get_index(i));
		}
	}
}

// The bitmap of a prefix range filter of a hash join, rebuilt through its lookups: the smallest and
// the largest key with a set bit by bisection, then the bit of every key of [min, max] (shift 0) or
// whether any key of a bucket of 2^shift keys has its bit set, so the bitmap folds when [min, max]
// does not fit. With bloom_words a filter that folds goes as the Bloom filter of its keys in at most
// that many words instead, if that passes fewer values. Not exact: the join checks the rows again.
template <class T>
static std::shared_ptr<const HwBitmap> PrefixRangeBitmap(const PrefixRangeFilter &prf, const LogicalType &key_type,
                                                         oasis::FilterTable &table, uint64_t bloom_words) {
	auto any = [&](int64_t lo, int64_t hi) {
		return prf.LookupRange(Value::CreateValue(T(lo)), Value::CreateValue(T(hi))) !=
		       FilterPropagateResult::FILTER_ALWAYS_FALSE;
	};
	const int64_t key_lo = NumericLimits<T>::Minimum();
	const int64_t key_hi = NumericLimits<T>::Maximum();
	if (!prf.IsInitialized() || !any(key_lo, key_hi)) {
		return nullptr;
	}
	auto mid = [](int64_t a, int64_t b, bool up) {
		return int64_t(uint64_t(a) + (uint64_t(b) - uint64_t(a) + (up ? 1 : 0)) / 2);
	};
	int64_t a = key_lo, b = key_hi;
	while (a < b) {
		const int64_t m = mid(a, b, false);
		any(key_lo, m) ? b = m : a = m + 1;
	}
	const int64_t min = a;
	b = key_hi;
	while (a < b) {
		const int64_t m = mid(a, b, true);
		any(m, key_hi) ? a = m : b = m - 1;
	}
	const int64_t max = a;

	const auto fold = FoldShift(min, max, BitmapBudget(table), oasis::FILTER_LOOKUP_MAX_SHIFT);
	if (!fold) {
		return MakeBitmap(min, max, 0, false, {}); // The interval alone
	}
	const uint32_t shift = *fold;

	const uint64_t num_bits = BitmapBits(min, max, shift);
	std::vector<uint64_t> words((num_bits + 63) / 64, 0);
	if (shift == 0) {
		ForEachKey<T>(prf, key_type, min, num_bits, [&](uint64_t bit) { SetBit(words, bit); });
	} else {
		// The keys of a span this wide are sparse: the buckets of a word are looked up only if the
		// word has a key.
		auto bucket_lo = [&](uint64_t bit) {
			return int64_t(uint64_t(min) + (bit << shift));
		};
		auto bucket_hi = [&](uint64_t bit) {
			return bit + 1 == num_bits ? max : int64_t(uint64_t(bucket_lo(bit)) + (uint64_t(1) << shift) - 1);
		};
		for (uint64_t first = 0; first < num_bits; first += 64) {
			const uint64_t last = std::min(first + 64, num_bits) - 1;
			if (!any(bucket_lo(first), bucket_hi(last))) {
				continue;
			}
			for (uint64_t bit = first; bit <= last; bit++) {
				if (any(bucket_lo(bit), bucket_hi(bit))) {
					SetBit(words, bit);
				}
			}
		}
	}
	auto folded = MakeBitmap(min, max, shift, false, std::move(words));
	if (shift == 0 || bloom_words == 0 || uint64_t(max) - uint64_t(min) >= KEYS_BLOOM_MAX_SPAN) {
		return folded;
	}

	// The keys go into the largest Bloom filter that fits, which KeysBloom folds to their number
	std::vector<uint64_t> bloom_bits(bloom_words, 0);
	uint64_t num_keys = 0;
	ForEachKey<T>(prf, key_type, min, uint64_t(max) - uint64_t(min) + 1, [&](uint64_t i) {
		BloomInsert(bloom_bits, Hash<T>(T(uint64_t(min) + i)));
		num_keys++;
	});
	// The one that passes fewer values of [min, max]: the keys and the false positives of the Bloom filter
	auto keys_bloom = KeysBloom(min, max, bloom_bits, num_keys);
	if (keys_bloom && keys_bloom->PassFraction(min, max) < folded->PassFraction(min, max)) {
		return keys_bloom;
	}
	return folded;
}

// The bitmap of `column IN (constants)` over the span of the stored values. An exact bitmap has to fit
// unfolded; else the bitmap folds like the one of a prefix range.
static std::shared_ptr<const HwBitmap> InListBitmap(const BoundOperatorExpression &in, const LogicalType &scan_type,
                                                    bool exact, oasis::FilterTable &table) {
	std::vector<int64_t> values;
	for (idx_t c = 1; c < in.GetChildren().size(); c++) {
		auto stored = StoredConstant(*in.GetChildren()[c], scan_type);
		if (!stored) {
			return nullptr;
		}
		values.push_back(*stored);
	}
	if (values.empty()) {
		return nullptr;
	}
	const auto [min_it, max_it] = std::minmax_element(values.begin(), values.end());
	const int64_t min = *min_it, max = *max_it;
	const uint64_t budget = exact ? BitmapBudget(table) : std::min(BitmapBudget(table), FOLDED_IN_LIST_MAX_BITS);
	const auto fold = FoldShift(min, max, budget, exact ? 0 : oasis::FILTER_LOOKUP_MAX_SHIFT);
	if (!fold) {
		return exact ? nullptr : MakeBitmap(min, max, 0, false, {}); // Nothing, or the interval alone
	}
	const uint32_t shift = *fold;
	std::vector<uint64_t> words((BitmapBits(min, max, shift) + 63) / 64, 0);
	for (auto value : values) {
		SetBit(words, (uint64_t(value) - uint64_t(min)) >> shift);
	}
	return MakeBitmap(min, max, shift, exact, std::move(words));
}

std::shared_ptr<const HwBitmap> LookupBitmap(ClientContext &context, oasis::FilterTable &table, const Expression &expr,
                                             const LogicalType &scan_type, bool optional, bool bloom) {
	if (expr.GetExpressionClass() == ExpressionClass::BOUND_FUNCTION) {
		auto &func = expr.Cast<BoundFunctionExpression>();
		const auto &name = func.Function().GetName();
		if (!func.BindInfo() || !ExpressionFilter::IsSimpleFilterColumnRef(*func.GetChildren()[0])) {
			return nullptr;
		}
		const auto physical = scan_type.InternalType();
		if (physical != PhysicalType::INT32 && physical != PhysicalType::INT64) {
			return nullptr;
		}
		const uint64_t bloom_words = bloom ? BloomWords(table) : 0;
		if (name == PrefixRangeScalarFun::NAME) {
			auto &data = func.BindInfo()->Cast<PrefixRangeFunctionData>();
			if (!data.filter || data.key_type != scan_type) {
				return nullptr;
			}
			if (physical == PhysicalType::INT32) {
				return PrefixRangeBitmap<int32_t>(*data.filter, scan_type, table, bloom_words);
			}
			return PrefixRangeBitmap<int64_t>(*data.filter, scan_type, table, bloom_words);
		}
		if (name == BloomFilterScalarFun::NAME && bloom) {
			auto &data = func.BindInfo()->Cast<BloomFilterFunctionData>();
			if (!data.filter || data.key_type != scan_type) {
				return nullptr;
			}
			return JoinBloom(context, *data.filter, scan_type, bloom_words);
		}
		return nullptr;
	}
	if (expr.GetExpressionClass() == ExpressionClass::BOUND_OPERATOR &&
	    expr.GetExpressionType() == ExpressionType::COMPARE_IN) {
		auto &in_list = expr.Cast<BoundOperatorExpression>();
		if (!ExpressionFilter::IsSimpleFilterColumnRef(*in_list.GetChildren()[0])) {
			return nullptr;
		}
		return InListBitmap(in_list, scan_type, !optional, table);
	}
	return nullptr;
}

} // namespace duckdb

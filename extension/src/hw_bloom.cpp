#include "hw_bloom.hpp"

#include "duckdb/common/limits.hpp"
#include "duckdb/common/types/hash.hpp"
#include "duckdb/logging/log_manager.hpp"
#include "hw_bitmap.hpp"

#include <cmath>

namespace duckdb {

// The private bits of DuckDB's BloomFilter. DuckDB has no accessor for them, and the hardware needs
// a copy. An explicit instantiation may name private members (the access check does not apply to
// its template arguments), so BloomFilterAccess takes their member pointers and its friend
// functions read them. A rename in DuckDB fails to compile here; JoinBloom checks the meaning of the
// bits against BloomFilter::LookupOne.
namespace {

template <auto Bits, auto NumSectors>
struct BloomFilterAccess {
	friend const uint64_t *BloomFilterBits(const BloomFilter &filter) {
		return filter.*Bits;
	}
	friend idx_t BloomFilterNumSectors(const BloomFilter &filter) {
		return filter.*NumSectors;
	}
};

template struct BloomFilterAccess<&BloomFilter::bf, &BloomFilter::num_sectors>;

const uint64_t *BloomFilterBits(const BloomFilter &filter);
idx_t BloomFilterNumSectors(const BloomFilter &filter);

} // namespace

// The sectors of DuckDB's filter whose copy JoinBloom checks against LookupOne.
static constexpr idx_t BLOOM_CHECK_SECTORS = 64;

// The four bits of the word that a hash tests: the low 6 bits of its bytes 4 to 7 (GetMask of DuckDB).
static uint64_t BloomMask(hash_t hash) {
	uint64_t mask = 0;
	for (idx_t byte = 4; byte < 8; byte++) {
		mask |= uint64_t(1) << ((hash >> (8 * byte)) & 63);
	}
	return mask;
}

void BloomInsert(std::vector<uint64_t> &words, hash_t hash) {
	words[hash & (words.size() - 1)] |= BloomMask(hash);
}

// The Bloom filter of num_sectors words folded to num_words words, both powers of two, as
// BloomFilter::Fold of DuckDB.
static std::vector<uint64_t> FoldBloom(const uint64_t *sectors, uint64_t num_sectors, uint64_t num_words) {
	std::vector<uint64_t> words(num_words, 0);
	for (uint64_t s = 0; s < num_sectors; s++) {
		words[s & (num_words - 1)] |= sectors[s];
	}
	return words;
}

// The Bloom filter of `words` (a power of two) over [min, max] with num_keys keys, the bitmap of a
// Bloom filter lookup, or nullptr when it passes about every value: a Bloom filter that passes a
// larger share of the values outside its keys than FOLDED_BITMAP_MAX_ONES is not worth the table,
// its interval goes to the hardware alone. Without num_keys the set bits give the estimate. exact:
// the words pass the values of the filter and no other (HwBitmap::exact).
static std::shared_ptr<const HwBitmap> MakeBloom(int64_t min, int64_t max, std::vector<uint64_t> words, bool exact,
                                                 std::optional<uint64_t> num_keys = std::nullopt) {
	D_ASSERT(IsPowerOfTwo(words.size()));
	uint64_t ones = 0;
	for (auto word : words) {
		ones += __builtin_popcountll(word);
	}
	// A value outside the keys passes if its 4 bits are set, the estimate of BloomFilter::Seal
	const double density = double(ones) / double(words.size() * 64);
	const double pass = std::pow(density, 4);
	if (pass > FOLDED_BITMAP_MAX_ONES) {
		return nullptr;
	}
	// A key leaves a bit of a word clear with probability 1 - (1 - (63/64)^4) / words, so n keys
	// leave the share (1 - density) = exp(-n (1 - (63/64)^4) / words)
	const double bits_per_key = 1 - std::pow(63.0 / 64.0, 4);
	const double keys = num_keys ? double(*num_keys) : -std::log1p(-density) * double(words.size()) / bits_per_key;
	auto bitmap = std::make_shared<HwBitmap>();
	bitmap->min = min;
	bitmap->max = max;
	bitmap->shift = 0;
	bitmap->words = std::move(words);
	bitmap->bloom = true;
	bitmap->bloom_pass = pass;
	bitmap->bloom_keys = keys;
	bitmap->exact = exact;
	return bitmap;
}

std::shared_ptr<const HwBitmap> JoinBloom(ClientContext &context, const BloomFilter &filter,
                                          const LogicalType &key_type, uint64_t max_words) {
	if (max_words == 0 || !filter.IsInitialized() || !filter.IsSealed()) {
		return nullptr;
	}
	const uint64_t *bits = BloomFilterBits(filter);
	const idx_t num_sectors = BloomFilterNumSectors(filter);

	// The copy has to read as LookupOne does: a hash with sector s and its four bit positions at b tests
	// bit b of sector s, and sector s + num_sectors is sector s again
	auto lookup_bit = [&](uint64_t sector, uint64_t bit) {
		return filter.LookupOne(sector | bit << 32 | bit << 40 | bit << 48 | bit << 56);
	};
	const idx_t step = std::max<idx_t>(1, num_sectors / BLOOM_CHECK_SECTORS);
	for (idx_t s = 0; s < num_sectors; s += step) {
		for (uint64_t b = 0; b < 64; b++) {
			const bool set = (bits[s] >> b) & 1;
			if (lookup_bit(s, b) != set || lookup_bit(s + num_sectors, b) != set) {
				DUCKDB_LOG_DEBUG(context, "Bloom filter on the CPU: its copy differs from LookupOne at sector %llu",
				                 (unsigned long long)s);
				return nullptr;
			}
		}
	}

	const bool is_64 = key_type.InternalType() == PhysicalType::INT64;
	const int64_t min = is_64 ? NumericLimits<int64_t>::Minimum() : NumericLimits<int32_t>::Minimum();
	const int64_t max = is_64 ? NumericLimits<int64_t>::Maximum() : NumericLimits<int32_t>::Maximum();
	// Exact if it fits unfolded: the hardware then looks up the words of DuckDB's filter
	return MakeBloom(min, max, FoldBloom(bits, num_sectors, std::min<uint64_t>(num_sectors, max_words)),
	                 num_sectors <= max_words);
}

std::shared_ptr<const HwBitmap> KeysBloom(int64_t min, int64_t max, const std::vector<uint64_t> &words,
                                          uint64_t num_keys) {
	const uint64_t num_words = std::min<uint64_t>(BloomFilter::GetNumberOfSectors(num_keys), words.size());
	return MakeBloom(min, max, FoldBloom(words.data(), words.size(), num_words), false, num_keys);
}

} // namespace duckdb

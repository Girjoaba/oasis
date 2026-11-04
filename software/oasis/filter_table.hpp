#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <vector>

namespace oasis {

class FilterTable;

/**
 * The words of one bitmap in the lookup table of the FilterStages (FilterTable in hardware): bit
 * b of the bitmap is bit b % 64 of words[b / 64], at word base + b / 64 of the table. The region
 * goes back to its FilterTable when the last reference goes, so the FilterSpec of every flow that
 * looks it up holds one.
 *
 * The words reach the hardware with the first flow that looks the region up: FilterConfig::enqueue
 * writes them on the dispatcher thread, before its FilterSpec, and sets written.
 */
class FilterTableRegion {
  public:
    FilterTableRegion(std::shared_ptr<FilterTable> table, size_t base, std::vector<uint64_t> words);
    ~FilterTableRegion();

    FilterTableRegion(const FilterTableRegion &) = delete;
    FilterTableRegion &operator=(const FilterTableRegion &) = delete;

    size_t                       base() const { return base_; }
    const std::vector<uint64_t> &words() const { return words_; }

    bool written = false; // Only the dispatcher thread reads and sets it

  private:
    std::shared_ptr<FilterTable> table_;
    size_t                       base_;
    std::vector<uint64_t>        words_;
};

/**
 * Splits the lookup table of the FilterStages into regions, one per bitmap. The table is the same
 * in every decoder (the hardware writes each word into all of them), so a region serves the row
 * groups on any decoder. First fit over the free words.
 */
class FilterTable : public std::enable_shared_from_this<FilterTable> {
  public:
    /**
     * A table of `bits` bits, 0 without lookup table in the bitstream.
     */
    explicit FilterTable(size_t bits);

    size_t words() const { return words_; }

    /**
     * A region for `words`, or nullptr when no free range of the table is long enough.
     */
    std::shared_ptr<FilterTableRegion> allocate(std::vector<uint64_t> words);

    /**
     * The longest free range of the table, in words.
     */
    size_t largest_free_words() const;

  private:
    friend class FilterTableRegion;
    void release(size_t base, size_t size);

    size_t                   words_;
    mutable std::mutex       mutex_;
    std::map<size_t, size_t> free_; // base -> size, in words
};

} // namespace oasis

#include <oasis/filter_table.hpp>

#include <algorithm>
#include <cassert>
#include <iterator>

namespace oasis {

FilterTableRegion::FilterTableRegion(std::shared_ptr<FilterTable> table, size_t base,
                                     std::vector<uint64_t> words)
    : table_(std::move(table)), base_(base), words_(std::move(words)) {}

FilterTableRegion::~FilterTableRegion() { table_->release(base_, words_.size()); }

FilterTable::FilterTable(size_t bits) : words_(bits / 64) {
    if (words_ > 0) {
        free_[0] = words_;
    }
}

std::shared_ptr<FilterTableRegion> FilterTable::allocate(std::vector<uint64_t> words) {
    if (words.empty()) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    for (auto it = free_.begin(); it != free_.end(); ++it) {
        auto [base, size] = *it;
        if (size < words.size()) {
            continue;
        }
        free_.erase(it);
        if (size > words.size()) {
            free_[base + words.size()] = size - words.size();
        }
        used_ += words.size();
        return std::make_shared<FilterTableRegion>(shared_from_this(), base, std::move(words));
    }
    return nullptr;
}

size_t FilterTable::largest_free_words() const {
    std::lock_guard<std::mutex> lock(mutex_);
    size_t largest = 0;
    for (const auto &[base, size] : free_) {
        largest = std::max(largest, size);
    }
    return largest;
}

size_t FilterTable::words_in_use() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return used_;
}

void FilterTable::release(size_t base, size_t size) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto [it, inserted] = free_.emplace(base, size);
    assert(inserted);
    used_ -= size;

    // Merge with the free ranges on both sides
    auto next = std::next(it);
    if (next != free_.end() && it->first + it->second == next->first) {
        it->second += next->second;
        free_.erase(next);
    }
    if (it != free_.begin()) {
        auto prev = std::prev(it);
        if (prev->first + prev->second == it->first) {
            prev->second += it->second;
            free_.erase(it);
        }
    }
}

} // namespace oasis

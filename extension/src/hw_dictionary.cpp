#include "hw_dictionary.hpp"

#include "duckdb/common/allocator.hpp"
#include "duckdb/logging/log_manager.hpp"
#include "duckdb/planner/filter/expression_filter.hpp"
#include "duckdb/planner/filter/table_filter_functions.hpp"
#include "duckdb/storage/object_cache.hpp"
#include "duckdb/storage/table/column_segment.hpp"
#include "hw_bitmap.hpp"
#include "parquet_reader.hpp"
#include "reader/string_column_reader.hpp"
#include "snappy.h"
#include "thrift/protocol/TCompactProtocol.h"
#include "thrift/transport/TBufferTransports.h"

#include <algorithm>

namespace duckdb {

using duckdb_apache::thrift::protocol::TCompactProtocolFactoryT;
using duckdb_apache::thrift::transport::TMemoryBuffer;
using duckdb_parquet::Encoding;
using duckdb_parquet::PageType;

// Whether every data page of the chunk is dictionary-encoded. Without encoding_stats the encodings
// must not have PLAIN, which some writers also list for the dictionary page.
static bool DataPagesDictionaryEncoded(const duckdb_parquet::ColumnMetaData &meta) {
	if (meta.__isset.encoding_stats) {
		for (const auto &stats : meta.encoding_stats) {
			if (stats.page_type == PageType::DICTIONARY_PAGE) {
				continue;
			}
			if (stats.page_type != PageType::DATA_PAGE ||
			    (stats.encoding != Encoding::PLAIN_DICTIONARY && stats.encoding != Encoding::RLE_DICTIONARY)) {
				return false;
			}
		}
		return true;
	}
	for (auto encoding : meta.encodings) {
		if (encoding != Encoding::PLAIN_DICTIONARY && encoding != Encoding::RLE_DICTIONARY &&
		    encoding != Encoding::RLE && encoding != Encoding::BIT_PACKED) {
			return false;
		}
	}
	return true;
}

void BuildDictionaryChunks(ParquetReader &reader, OasisScanBindData &bind) {
	auto *file_meta = reader.GetFileMetadata();
	bind.dict_chunks.assign(bind.metadata.groups.size(), {});
	for (size_t g = 0; g < bind.metadata.groups.size(); g++) {
		const auto &chunks = bind.metadata.groups[g].chunks;
		bind.dict_chunks[g].resize(chunks.size());
		for (size_t c = 0; c < chunks.size(); c++) {
			const auto &cc = chunks[c];
			const auto &meta = file_meta->row_groups[g].columns[c].meta_data;
			// A plain VARCHAR only: an alias such as JSON validates its values in the reader
			if (cc.type != parcore::metadata::Type::BYTE_ARRAY || bind.column_types[c].id() != LogicalTypeId::VARCHAR ||
			    bind.column_types[c].HasAlias() || !meta.__isset.dictionary_page_offset || meta.dictionary_page_offset <= 0 ||
			    meta.data_page_offset <= meta.dictionary_page_offset || !DataPagesDictionaryEncoded(meta)) {
				continue;
			}
			const bool no_nulls = cc.is_required || (meta.__isset.statistics && meta.statistics.__isset.null_count &&
			                                         meta.statistics.null_count == 0);
			if (!no_nulls) {
				continue;
			}
			DictionaryChunk dict;
			dict.dict_offset = uint64_t(meta.dictionary_page_offset);
			dict.dict_size = uint64_t(meta.data_page_offset - meta.dictionary_page_offset);
			dict.ids = cc;
			dict.ids.type = parcore::metadata::Type::INT32_T;
			dict.ids.logical_width = 0;
			dict.ids.offset = uint64_t(meta.data_page_offset);
			dict.ids.total_compressed_size = cc.total_compressed_size - dict.dict_size;
			bind.dict_chunks[g][c] = std::move(dict);
		}
	}
}

void InitDictIdFilters(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate) {
	for (auto &scan_filter : lstate.scan_filters) {
		if (!gstate.projected_columns[scan_filter.filter_idx].dict_ids ||
		    scan_filter.filter.filter_type != TableFilterType::EXPRESSION_FILTER) {
			continue;
		}
		const auto &expr_filter = scan_filter.filter.Cast<ExpressionFilter>();
		const auto &expr = *expr_filter.expr;
		// A top-N bound tightens during the scan
		if (ExpressionFilter::ContainsInternalFunction(expr, DynamicFilterScalarFun::NAME)) {
			continue;
		}
		auto child = ExpressionFilter::GetOptionalFilterChild(expr);
		auto dict_id_filter = child ? make_uniq<ExpressionFilter>(child->Copy()) : expr_filter.Copy();
		auto state = TableFilterState::Initialize(context, *dict_id_filter);
		// The entries of a dictionary are not all in the rows, so a filter that can throw stays on them
		if (state->can_throw) {
			continue;
		}
		scan_filter.dict_id_filter = std::move(dict_id_filter);
		scan_filter.dict_id_filter_state = std::move(state);
		scan_filter.dict_id_optional = child != nullptr;
	}
}

// The dictionary page of the chunk (dict.dict_size bytes at `page`) decoded into a reusable dictionary: PLAIN
// BYTE_ARRAY entries, raw or SNAPPY. `memory` is about the bytes the dictionary takes.
static GroupDictionary DecodeDictionary(data_ptr_t page, const DictionaryChunk &dict, const LogicalType &type,
                                        const string &column_name, idx_t &memory) {
	auto &allocator = Allocator::DefaultAllocator();
	auto transport = duckdb_base_std::make_shared<TMemoryBuffer>(page, uint32_t(dict.dict_size));
	TCompactProtocolFactoryT<TMemoryBuffer> factory;
	auto protocol = factory.getProtocol(transport);
	duckdb_parquet::PageHeader header;
	header.read(protocol.get());
	const idx_t header_size = dict.dict_size - transport->available_read();
	if (header.type != PageType::DICTIONARY_PAGE || !header.__isset.dictionary_page_header ||
	    header.dictionary_page_header.num_values < 0 ||
	    (header.dictionary_page_header.encoding != Encoding::PLAIN &&
	     header.dictionary_page_header.encoding != Encoding::PLAIN_DICTIONARY) ||
	    header.compressed_page_size < 0 || header_size + header.compressed_page_size > dict.dict_size) {
		throw InvalidInputException("read_oasis: column '%s' has no PLAIN dictionary page at offset %llu",
		                            column_name.c_str(), (unsigned long long)dict.dict_offset);
	}
	const idx_t num_entries = idx_t(header.dictionary_page_header.num_values);
	if (num_entries > MAX_DICTIONARY_IDS) {
		throw NotImplementedException("read_oasis: the dictionary of column '%s' has %llu entries, the decoders emit "
		                              "at most %llu ids",
		                              column_name.c_str(), (unsigned long long)num_entries,
		                              (unsigned long long)MAX_DICTIONARY_IDS);
	}

	const_data_ptr_t body = page + header_size;
	idx_t body_size = idx_t(header.compressed_page_size);
	AllocatedData uncompressed;
	if (dict.ids.compression == parcore::metadata::Compression::SNAPPY) {
		size_t uncompressed_size;
		if (!duckdb_snappy::GetUncompressedLength(const_char_ptr_cast(body), body_size, &uncompressed_size) ||
		    uncompressed_size != size_t(header.uncompressed_page_size)) {
			throw InvalidInputException("read_oasis: corrupt SNAPPY dictionary page of column '%s'",
			                            column_name.c_str());
		}
		uncompressed = allocator.Allocate(uncompressed_size);
		if (!duckdb_snappy::RawUncompress(const_char_ptr_cast(body), body_size, char_ptr_cast(uncompressed.get()))) {
			throw InvalidInputException("read_oasis: corrupt SNAPPY dictionary page of column '%s'",
			                            column_name.c_str());
		}
		body = uncompressed.get();
		body_size = uncompressed_size;
	}

	GroupDictionary result;
	result.size = num_entries;
	result.entry = DictionaryVector::CreateReusableDictionary(type, num_entries);
	auto &data = result.entry->data;
	FlatVector::ValidityMutable(data).Reset(num_entries);
	auto strings = FlatVector::GetDataMutable<string_t>(data);
	idx_t pos = 0;
	for (idx_t e = 0; e < num_entries; e++) {
		uint32_t length;
		if (pos + sizeof(length) > body_size) {
			throw InvalidInputException("read_oasis: truncated dictionary page of column '%s'", column_name.c_str());
		}
		memcpy(&length, body + pos, sizeof(length));
		pos += sizeof(length);
		if (length > body_size - pos) {
			throw InvalidInputException("read_oasis: truncated dictionary page of column '%s'", column_name.c_str());
		}
		auto str = const_char_ptr_cast(body + pos);
		if (!StringColumnReader::IsValid(str, length, true)) {
			throw InvalidInputException("read_oasis: invalid string encoding in the dictionary of column '%s'",
			                            column_name.c_str());
		}
		strings[e] = StringVector::AddStringOrBlob(data, str, length);
		pos += length;
	}
	memory = sizeof(DictionaryEntry) + num_entries * sizeof(string_t) + body_size;
	return result;
}

// A decoded dictionary in DuckDB's object cache, shared by the scans of every connection of the database:
// the cache is thread-safe, and the scans only read the dictionary. The cache bounds its memory and drops
// the least recently used entries first, also when the buffer pool needs the room.
namespace {
struct DictionaryCacheEntry : public ObjectCacheEntry {
	DictionaryCacheEntry(GroupDictionary dictionary_p, idx_t memory_p)
	    : dictionary(std::move(dictionary_p)), memory(memory_p) {
	}

	static string ObjectType() {
		return "oasis_dictionary";
	}
	string GetObjectType() override {
		return ObjectType();
	}
	optional_idx GetEstimatedCacheMemory() const override {
		return optional_idx(memory);
	}

	GroupDictionary dictionary;
	idx_t memory;
};
} // namespace

// The key of the dictionary page of the chunk: the file, its offset and its size.
static string DictionaryCacheKey(const OasisScanLocalState &lstate, const DictionaryChunk &dict) {
	return StringUtil::Format("%s:%llu:%llu", lstate.dict_cache_file, (unsigned long long)dict.dict_offset,
	                          (unsigned long long)dict.dict_size);
}

// DecodeDictionary, and the dictionary into the cache.
static GroupDictionary DecodeAndCacheDictionary(ClientContext &context, const OasisScanLocalState &lstate,
                                                data_ptr_t page, const DictionaryChunk &dict, const LogicalType &type,
                                                const string &column_name) {
	idx_t memory = 0;
	auto dictionary = DecodeDictionary(page, dict, type, column_name, memory);
	if (!lstate.dict_cache_file.empty()) {
		ObjectCache::GetObjectCache(context).PutWithTypePrefix<DictionaryCacheEntry>(
		    DictionaryCacheKey(lstate, dict), make_shared_ptr<DictionaryCacheEntry>(dictionary, memory));
	}
	return dictionary;
}

// Whether a conjunct of dictionary id column i still filters the rows of the group, so that
// PrepareDictIdColumns evaluates it over the dictionary before the group is submitted.
static bool DictionaryNeededAtSubmit(const OasisScanLocalState &lstate,
                                     const OasisScanLocalState::PendingGroup &pending, size_t i) {
	for (size_t f = 0; f < lstate.scan_filters.size(); f++) {
		const auto &scan_filter = lstate.scan_filters[f];
		if (scan_filter.filter_idx == i && scan_filter.dict_id_filter && pending.needs_row_filter[f]) {
			return true;
		}
	}
	return false;
}

bool LookupDictionaries(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                        const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending, bool fetch) {
	if (!gstate.has_dict_id_columns) {
		return false;
	}
	ScopedTimer timer(lstate.string_decode_time_ns);
	const size_t num_cols = gstate.projected_columns.size();
	pending.dictionaries.assign(num_cols, {});
	pending.dict_buffers.assign(num_cols, {});
	pending.dict_slot.clear();
	auto &cache = ObjectCache::GetObjectCache(context);
	bool needed_at_submit = false;
	for (size_t i = 0; i < num_cols; i++) {
		const auto &col = gstate.projected_columns[i];
		if (!col.dict_ids) {
			continue;
		}
		const auto &name = bind.metadata.column_names[col.column_id];
		const auto &dict = *bind.dict_chunks[pending.group][col.column_id];
		if (!lstate.dict_cache_file.empty()) {
			auto cached = cache.GetWithTypePrefix<DictionaryCacheEntry>(DictionaryCacheKey(lstate, dict));
			if (cached) {
				pending.dictionaries[i] = cached->dictionary;
				lstate.dict_pages_cached++;
				DUCKDB_LOG_DEBUG(context, "Row group %llu: the dictionary of column '%s' is in the cache",
				                 (unsigned long long)pending.group, name.c_str());
				continue;
			}
		}
		lstate.dict_pages_fetched++;
		if (fetch) {
			pending.dict_slot.push_back(i);
			needed_at_submit = needed_at_submit || DictionaryNeededAtSubmit(lstate, pending, i);
			continue;
		}
		auto page = Allocator::DefaultAllocator().Allocate(dict.dict_size);
		lstate.file_handle->Read(page.get(), dict.dict_size, dict.dict_offset);
		pending.dictionaries[i] =
		    DecodeAndCacheDictionary(context, lstate, page.get(), dict, bind.output_types[col.column_id], name);
	}
	return needed_at_submit;
}

void DecodeFetchedDictionaries(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                               const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending) {
	if (pending.dict_slot.empty()) {
		return;
	}
	ScopedTimer timer(lstate.string_decode_time_ns);
	for (size_t i : pending.dict_slot) {
		const auto &col = gstate.projected_columns[i];
		const auto &dict = *bind.dict_chunks[pending.group][col.column_id];
		auto &buffers = pending.dict_buffers[i];
		// The bypass stream completes the buffers of a sink in order, so the batches hold the page in file
		// order. A page in one buffer is decoded in place.
		data_ptr_t page;
		AllocatedData joined;
		if (buffers.size() == 1 && buffers[0]->size == dict.dict_size) {
			page = static_cast<data_ptr_t>(buffers[0]->ptr);
		} else {
			joined = Allocator::DefaultAllocator().Allocate(dict.dict_size);
			idx_t copied = 0;
			for (const auto &buf : buffers) {
				if (copied + buf->size > dict.dict_size) {
					throw IOException("Row group %llu: the dictionary page of column %llu overran its %llu bytes",
					                  (unsigned long long)pending.group, (unsigned long long)i,
					                  (unsigned long long)dict.dict_size);
				}
				memcpy(joined.get() + copied, buf->ptr, buf->size);
				copied += buf->size;
			}
			if (copied != dict.dict_size) {
				throw IOException("Row group %llu: the dictionary page of column %llu has %llu of its %llu bytes",
				                  (unsigned long long)pending.group, (unsigned long long)i,
				                  (unsigned long long)copied, (unsigned long long)dict.dict_size);
			}
			page = joined.get();
		}
		pending.dictionaries[i] = DecodeAndCacheDictionary(context, lstate, page, dict, bind.output_types[col.column_id],
		                                                   bind.metadata.column_names[col.column_id]);
		buffers.clear();
	}
	pending.dict_slot.clear();
}

// What the hardware takes of the passing ids: one interval of ids, the ids outside one interval
// (negate), or their bitmap over [0, size - 1].
static HwScanFilter DictIdsHardwareFilter(const std::vector<bool> &pass, idx_t count) {
	HwScanFilter hw;
	const int64_t size = int64_t(pass.size());
	if (count == 0) {
		hw.interval = HwInterval {1, 0, false};
		return hw;
	}
	auto first = std::find(pass.begin(), pass.end(), true) - pass.begin();
	auto last = size - 1 - (std::find(pass.rbegin(), pass.rend(), true) - pass.rbegin());
	if (last - first + 1 == int64_t(count)) {
		hw.interval = HwInterval {first, last, false};
		return hw;
	}
	auto first_fail = std::find(pass.begin(), pass.end(), false) - pass.begin();
	auto last_fail = size - 1 - (std::find(pass.rbegin(), pass.rend(), false) - pass.rbegin());
	if (last_fail - first_fail + 1 == size - int64_t(count)) {
		hw.interval = HwInterval {first_fail, last_fail, true};
		return hw;
	}
	std::vector<uint64_t> words((pass.size() + 63) / 64, 0);
	for (size_t id = 0; id < pass.size(); id++) {
		if (pass[id]) {
			SetBit(words, id);
		}
	}
	hw.bitmap = MakeBitmap(0, size - 1, 0, true, std::move(words));
	hw.interval = HwInterval {0, size - 1, false};
	return hw;
}

bool PrepareDictIdColumns(ClientContext &context, const OasisScanGlobalState &gstate, OasisScanLocalState &lstate,
                          const OasisScanBindData &bind, OasisScanLocalState::PendingGroup &pending) {
	if (!gstate.has_dict_id_columns) {
		return true;
	}
	ScopedTimer timer(lstate.string_decode_time_ns);
	const size_t num_cols = gstate.projected_columns.size();
	pending.dict_id_filters.assign(num_cols, nullptr);
	for (size_t i = 0; i < num_cols; i++) {
		const auto &col = gstate.projected_columns[i];
		// A column without a conjunct to evaluate gets its dictionary later, with the group's splinter
		if (!col.dict_ids || !DictionaryNeededAtSubmit(lstate, pending, i)) {
			continue;
		}
		const auto &name = bind.metadata.column_names[col.column_id];
		const auto &dictionary = pending.dictionaries[i];
		if (!dictionary.entry) {
			throw InternalException("Row group %llu: the dictionary of column '%s' is not in before the submit",
			                        (unsigned long long)pending.group, name.c_str());
		}

		// The conjuncts narrow one selection of the entries, as on the rows
		auto dict_id_filter = std::make_shared<DictIdFilter>();
		SelectionVector sel;
		idx_t count = dictionary.size;
		for (size_t f = 0; f < lstate.scan_filters.size(); f++) {
			auto &scan_filter = lstate.scan_filters[f];
			if (scan_filter.filter_idx != i || !scan_filter.dict_id_filter || !pending.needs_row_filter[f]) {
				continue;
			}
			if (count > 0) {
				ColumnSegment::FilterSelection(sel, dictionary.entry->data, *scan_filter.dict_id_filter_state,
				                               dictionary.size, count);
			}
			dict_id_filter->conjuncts.push_back(f);
			dict_id_filter->required = dict_id_filter->required || !scan_filter.dict_id_optional;
		}
		if (dict_id_filter->conjuncts.empty()) {
			continue;
		}
		dict_id_filter->pass.assign(dictionary.size, false);
		for (idx_t e = 0; e < count; e++) {
			dict_id_filter->pass[sel.get_index(e)] = true;
		}

		if (count == 0 && dict_id_filter->required) {
			DUCKDB_LOG_DEBUG(context, "Skipping row group %llu: no entry of the dictionary of column '%s' passes",
			                 (unsigned long long)pending.group, name.c_str());
			return false;
		}
		if (count == dictionary.size) {
			for (size_t f : dict_id_filter->conjuncts) {
				pending.needs_row_filter[f] = false;
			}
			continue;
		}
		dict_id_filter->pass_fraction = double(count) / double(dictionary.size);
		dict_id_filter->hw = DictIdsHardwareFilter(dict_id_filter->pass, count);
		DUCKDB_LOG_DEBUG(context, "Row group %llu: %llu of %llu dictionary ids of column '%s' pass",
		                 (unsigned long long)pending.group, (unsigned long long)count,
		                 (unsigned long long)dictionary.size, name.c_str());
		pending.dict_id_filters[i] = std::move(dict_id_filter);
	}
	return true;
}

void FilterDictIds(const Vector &vec, const DictIdFilter &filter, SelectionVector &sel, idx_t &approved_tuple_count) {
	const auto &ids = DictionaryVector::SelVector(vec);
	SelectionVector new_sel(approved_tuple_count);
	idx_t passed = 0;
	for (idx_t i = 0; i < approved_tuple_count; i++) {
		const auto row = sel.get_index(i);
		if (filter.pass[ids.get_index(row)]) {
			new_sel.set_index(passed++, row);
		}
	}
	sel.Initialize(new_sel);
	approved_tuple_count = passed;
}

} // namespace duckdb

#include "parcore_metadata_util.hpp"

#include "duckdb/common/exception.hpp"
#include "parquet_types.h"

namespace duckdb {

static parcore::metadata::Type parquet_type_to_parcore(duckdb_parquet::Type::type t) {
	switch (t) {
	case duckdb_parquet::Type::BOOLEAN:
		return parcore::metadata::Type::BYTE_T;
	case duckdb_parquet::Type::INT32:
		return parcore::metadata::Type::INT32_T;
	case duckdb_parquet::Type::FLOAT:
		return parcore::metadata::Type::FLOAT_T;
	case duckdb_parquet::Type::INT64:
		return parcore::metadata::Type::INT64_T;
	case duckdb_parquet::Type::DOUBLE:
		return parcore::metadata::Type::DOUBLE_T;
	case duckdb_parquet::Type::BYTE_ARRAY:
		return parcore::metadata::Type::BYTE_ARRAY;
	default:
		throw InvalidInputException("Parquet physical type %d not supported by ParCore", (int)t);
	}
}

static parcore::metadata::Compression parquet_codec_to_parcore(duckdb_parquet::CompressionCodec::type c) {
	switch (c) {
	case duckdb_parquet::CompressionCodec::UNCOMPRESSED:
		return parcore::metadata::Compression::RAW;
	case duckdb_parquet::CompressionCodec::SNAPPY:
		return parcore::metadata::Compression::SNAPPY;
	default:
		throw InvalidInputException("Parquet compression codec %d not supported by ParCore", (int)c);
	}
}

// Appends the max definition level of every leaf below schema[idx] in depth-first order, which is
// the order of the column chunks in a row group. Returns the index after the subtree.
static idx_t CollectLeafMaxDefine(const vector<duckdb_parquet::SchemaElement> &schema, idx_t idx,
                                  idx_t parent_max_define, vector<idx_t> &leaf_max_define) {
	if (idx >= schema.size()) {
		throw InvalidInputException("Malformed Parquet schema: child index %llu out of bounds", (unsigned long long)idx);
	}
	auto &element = schema[idx];
	auto max_define = parent_max_define;
	if (element.__isset.repetition_type && element.repetition_type != duckdb_parquet::FieldRepetitionType::REQUIRED) {
		max_define++;
	}

	idx = idx + 1;
	if (!element.__isset.num_children || element.num_children == 0) {
		leaf_max_define.push_back(max_define);
		return idx;
	}
	for (int32_t i = 0; i < element.num_children; i++) {
		idx = CollectLeafMaxDefine(schema, idx, max_define, leaf_max_define);
	}
	return idx;
}

parcore::metadata::Metadata BuildParcoreMetadata(ParquetReader &parquet_reader) {
	auto *file_meta = parquet_reader.GetFileMetadata();

	parcore::metadata::Metadata meta;

	// schema[0] is the root; its own repetition (if any) does not contribute definition levels.
	vector<idx_t> leaf_max_define;
	if (!file_meta->schema.empty()) {
		auto &root = file_meta->schema[0];
		for (idx_t idx = 1, child = 0; child < static_cast<idx_t>(root.num_children); child++) {
			idx = CollectLeafMaxDefine(file_meta->schema, idx, 0, leaf_max_define);
		}
	}

	for (auto &col : parquet_reader.columns) {
		meta.column_names.push_back(col.name.GetIdentifierName());
	}

	for (auto &rg : file_meta->row_groups) {
		parcore::metadata::RowGroup parcore_rg;

		if (rg.columns.size() != leaf_max_define.size()) {
			throw InvalidInputException("Parquet row group has %llu column chunks but the schema has %llu leaf columns",
			                            (unsigned long long)rg.columns.size(),
			                            (unsigned long long)leaf_max_define.size());
		}

		for (idx_t col_idx = 0; col_idx < rg.columns.size(); col_idx++) {
			auto &cmd = rg.columns[col_idx].meta_data;

			parcore::metadata::ColumnChunk parcore_cc;
			parcore_cc.type = parquet_type_to_parcore(cmd.type);
			parcore_cc.num_values = static_cast<uint64_t>(cmd.num_values);
			parcore_cc.compression = parquet_codec_to_parcore(cmd.codec);
			parcore_cc.offset = static_cast<uint64_t>(cmd.__isset.dictionary_page_offset ? cmd.dictionary_page_offset
			                                                                             : cmd.data_page_offset);
			parcore_cc.total_compressed_size = static_cast<uint64_t>(cmd.total_compressed_size);
			// The max definition level covers the whole path, so a REQUIRED leaf below an OPTIONAL group has
			// definition levels and is not is_required.
			parcore_cc.is_required = leaf_max_define[col_idx] == 0;

			parcore_rg.chunks.push_back(std::move(parcore_cc));
		}

		meta.groups.push_back(std::move(parcore_rg));
	}

	return meta;
}

} // namespace duckdb

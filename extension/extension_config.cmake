# This file is included by DuckDB's build system. It specifies which extension to load

# Extension from this repo
duckdb_extension_load(oasis
    SOURCE_DIR ${CMAKE_CURRENT_LIST_DIR}
    LOAD_TESTS
)

# DuckDB >= 2.0 only links extensions into the shell/library when explicitly requested
if(COMMAND duckdb_extension_statically_link)
    duckdb_extension_statically_link(oasis)
endif()

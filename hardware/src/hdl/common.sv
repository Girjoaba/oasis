package oasis;

import libstf::vaddress_t;
import libstf::size_t;
import libstf::data32_t;
import libstf::data64_t;

parameter longint unsigned OASIS_SYSTEM_ID = 64'h0A515;

parameter int NUM_READ_REQ_CONFIG_REGS = 3;
parameter longint unsigned READ_REQ_CONFIG_ID = 64'h2f966a70f04c0e93;

typedef struct packed {
    vaddress_t vaddr;
    size_t     len;
} read_req_t;

// -- FilterStage ----------------------------------------------------------------------------------
// Rows of the selection bitmap of one FilterStage: the rows of one row group whose selection the
// stage carries from its filter columns to its other columns (16 KiB of BRAM per decoder).
parameter int FILTER_SEL_ROWS = 2 ** 17;

// Bits of the lookup table of one FilterStage: the yes/no bitmaps of the lookup filters (the prefix
// range filters of the joins and the IN lists), one region per bitmap, in 64 bit words. The host
// writes the table once for all decoders, every decoder holds a copy (8 copies of 2 ** 14 words in
// URAM per decoder for 2 ** 20 bits). 0 removes the lookup.
`ifdef OASIS_FILTER_TABLE_BITS
parameter int FILTER_TABLE_BITS = `OASIS_FILTER_TABLE_BITS;
`else
parameter int FILTER_TABLE_BITS = 2 ** 20;
`endif
parameter int FILTER_TABLE_WORDS     = FILTER_TABLE_BITS / 64;
parameter int FILTER_TABLE_WORD_BITS = FILTER_TABLE_WORDS > 1 ? $clog2(FILTER_TABLE_WORDS) : 1;
parameter int FILTER_TABLE_LATENCY   = 4; // Enabled cycles from the word and bit positions to the bit

// Whether the FilterLookup also takes Bloom filters (the Bloom filters of the joins, as DuckDB builds
// them): a MurmurHash64 of every value (FilterHash) in place of the range index, then the same table
// read. Needs the lookup table. 0 removes the hash units (16 per decoder, two 64 bit multiplies each).
`ifdef OASIS_FILTER_BLOOM
parameter bit FILTER_BLOOM = `OASIS_FILTER_BLOOM;
`else
parameter bit FILTER_BLOOM = 1;
`endif
parameter int FILTER_HASH_MUL_STAGES = 4;                              // Registers after a multiply
parameter int FILTER_HASH_LATENCY    = 2 * FILTER_HASH_MUL_STAGES + 3; // Enabled cycles from the value to its hash

// The feature bits of read register 5 of the FilterConfig
parameter int FILTER_FEATURE_BLOOM = 0;

// Per decoder four write registers (lo, hi, ctrl, lut_min), then two write registers of the table
// (table_addr, table_data). Six read registers (ID, number of decoders, FIFO depth,
// FILTER_SEL_ROWS, FILTER_TABLE_BITS, features). The GlobalConfig sizes both sides with one value.
parameter int NUM_FILTER_CONFIG_WRITE_REGS = 4;
parameter int NUM_FILTER_TABLE_WRITE_REGS  = 2;
parameter int NUM_FILTER_CONFIG_READ_REGS  = 6;
parameter int MAX_NUM_ENQUEUED_FILTER_CONFS = 64;
parameter longint unsigned FILTER_CONFIG_ID = 64'h14e8418192d46a72;

function automatic int FILTER_CONFIG_REGS(input int num_decoders);
    return NUM_FILTER_CONFIG_WRITE_REGS * num_decoders + NUM_FILTER_TABLE_WRITE_REGS > NUM_FILTER_CONFIG_READ_REGS ?
           NUM_FILTER_CONFIG_WRITE_REGS * num_decoders + NUM_FILTER_TABLE_WRITE_REGS : NUM_FILTER_CONFIG_READ_REGS;
endfunction

// What the FilterStage does with one column chunk. The bits are the ctrl register of FilterConfig.
//   compare:   a value passes if lo <= x <= hi (negate: if not). Without compare every value passes.
//   load_sel:  AND the selection bitmap of the decoder into the pass bits (the previous filter
//              columns of the row group).
//   store_sel: write the pass bits into the selection bitmap.
//   emit:      write the passing values to the host. Without emit the output of the chunk is empty.
//   lookup:    a value passes only if its bit in the lookup table is set: bit
//              base * 64 + ((x - lut_min) >> shift), in the width of the value. Needs compare
//              without negate, with lo and hi inside the region of the bitmap.
//   bloom:     with lookup, the region holds a Bloom filter of 2 ** shift words, as DuckDB builds it:
//              with h the MurmurHash64 of x, a value passes only if the bits h[37:32], h[45:40],
//              h[53:48] and h[61:56] of word base + h[shift - 1:0] are set. Needs FILTER_BLOOM; the
//              compare is optional.
//   hash64:    the hash of an INT32 value is the hash of its 64 bit sign extension (a column of
//              64 bit keys whose chunk the decoder narrowed), else of its 32 bit zero extension.
// The bits 4 to 0 are compare to emit, bit 5 lookup, the bits 11 to 6 shift, bit 12 bloom, bit 13
// hash64, the bits 39 to 16 base.
typedef struct packed {
    logic[23:0] base; // Word of the region of the bitmap in the lookup table
    logic[1:0]  reserved;
    logic       hash64;
    logic       bloom;
    logic[5:0]  shift;
    logic       lookup;
    logic       compare;
    logic       negate;
    logic       load_sel;
    logic       store_sel;
    logic       emit;
} filter_ctrl_t;

typedef struct packed {
    filter_ctrl_t ctrl;
    data64_t      lo;
    data64_t      hi;
    data64_t      lut_min;
} filter_conf_t;

// One word write into the lookup tables of all decoders.
typedef struct packed {
    logic[FILTER_TABLE_WORD_BITS - 1:0] addr;
    data64_t                            data;
} filter_table_write_t;

// One 32 bit lane of the FilterCompact with the keep bits of its bytes, so that a partial last
// lane of a BYTE or INT16 column survives the compaction unchanged.
typedef struct packed {
    data32_t   data;
    logic[3:0] keep;
} filter_lane_t;

endpackage

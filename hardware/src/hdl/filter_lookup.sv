`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * Looks every value of a typed ndata beat up in the lookup table (FilterTable) and ANDs the result
 * into the pass bits of the FilterCompare. The beat, its pass bits and the selection bits of its
 * conf travel through the pipeline together, so the conf of the chunk may change once its last beat
 * has entered.
 *
 * With lookup, a value x of the conf passes if it passed the compare and the bit
 * base * 64 + ((x - lut_min) >> shift) of the table is set. The difference is taken in the width
 * of the value, INT32_T or INT64_T, so the software only has to keep the compare interval inside
 * the region of the bitmap. The index of a value that fails the compare may leave the region; its
 * pass bit stays 0. With lookup and bloom, x passes if the four bits of its MurmurHash64 h are set
 * in word base + h[shift - 1:0] (see filter_ctrl_t). Without lookup the pass bits pass through
 * unchanged.
 *
 * Lanes as in the FilterCompare: 16 values per beat for 32 bit types, 8 for 64 bit types (pass bits
 * [7:0]). The lane pair 2I, 2I + 1 holds two INT32 values or one INT64 value, which the odd lane of
 * the pair looks up.
 *
 * Pipeline: input register, the index of the table read, FILTER_TABLE_LATENCY cycles of the table,
 * then a skid buffer. The index of a bitmap takes three stages (difference, shift, index). With
 * FILTER_BLOOM the index of a Bloom filter takes the FilterHash and one stage for the word; the
 * bitmap index starts RANGE_START stages later to be ready with it, and one register takes either
 * index to the table. All stages move with one enable, which is low while a table word is written
 * (the write takes port A of the table) and while the output holds a beat that the skid buffer
 * cannot take. The skid buffer keeps the ready of the next stage off that enable. Full rate, one
 * beat per cycle except the cycles of the table writes.
 */
module FilterLookup
    import libstf::*;
    import oasis::*;
#(
    parameter DATABEAT_SIZE
) (
    input logic clk,
    input logic rst_n,

    input filter_conf_t                  conf, // Valid with in.valid
    input logic[DATABEAT_SIZE / 4 - 1:0] pass, // Valid with in.valid

    input logic                table_write_valid,
    input filter_table_write_t table_write,

    typed_ndata_i.s in,  // #(DATABEAT_SIZE)
    typed_ndata_i.m out, // #(DATABEAT_SIZE)

    output logic                          out_load_sel,  // Valid with out.valid
    output logic                          out_store_sel, // Valid with out.valid
    output logic                          out_emit,      // Valid with out.valid
    output logic[DATABEAT_SIZE / 4 - 1:0] out_pass       // Valid with out.valid
);

localparam NUM_32B_LANES = DATABEAT_SIZE / 4;
localparam NUM_64B_LANES = DATABEAT_SIZE / 8;
localparam INDEX_BITS    = FILTER_TABLE_WORD_BITS + 6;
localparam WIDE_BITS     = 32 + INDEX_BITS - 1; // The bits of a 64 bit difference a shift <= 31 can reach
localparam WORD_BITS     = FILTER_TABLE_WORD_BITS;
localparam RANGE_STAGES  = 3; // Difference, shift, index
localparam HASH_STAGES   = FILTER_HASH_LATENCY;
localparam INDEX_STAGES  = FILTER_BLOOM ? HASH_STAGES + 1 : RANGE_STAGES;
localparam RANGE_START   = FILTER_BLOOM ? HASH_STAGES - RANGE_STAGES : 0; // The stage of the difference
localparam NUM_STAGES    = 1 + INDEX_STAGES + FILTER_TABLE_LATENCY;

`ASSERT_ELAB(DATABEAT_SIZE % 8 == 0)
`ASSERT_ELAB(INDEX_BITS <= 32)
`ASSERT_ELAB(!FILTER_BLOOM || HASH_STAGES >= RANGE_STAGES)

typedef struct packed {
    data8_t[DATABEAT_SIZE - 1:0] data;
    logic[DATABEAT_SIZE - 1:0]   keep;
    type_t                       typ;
    logic                        last;
    logic[NUM_32B_LANES - 1:0]   pass;
    logic                        load_sel;
    logic                        store_sel;
    logic                        emit;
    logic                        lookup;
    logic                        bloom;
    logic                        hash64;
    logic[5:0]                   shift;
    logic[WORD_BITS - 1:0]       base;
    data64_t                     lut_min;
} beat_t;

`ifndef SYNTHESIS
assert property (@(posedge clk) disable iff (!rst_n)
    !(in.valid && conf.ctrl.lookup) || (in.typ == INT32_T || in.typ == INT64_T))
else $fatal(1, "FilterLookup does not support type %0d", in.typ);

assert property (@(posedge clk) disable iff (!rst_n)
    !(in.valid && conf.ctrl.lookup && !conf.ctrl.bloom) ||
    (conf.ctrl.compare && !conf.ctrl.negate && conf.ctrl.shift < 32))
else $fatal(1, "FilterLookup needs compare without negate and a shift below 32");

assert property (@(posedge clk) disable iff (!rst_n)
    !(in.valid && conf.ctrl.lookup && conf.ctrl.bloom) || (FILTER_BLOOM && conf.ctrl.shift <= WORD_BITS))
else $fatal(1, "FilterLookup has no Bloom filter or the Bloom filter exceeds the table");
`endif

// -- Pipeline -------------------------------------------------------------------------------------
ready_valid_i #(beat_t) skid_in(clk, rst_n), skid_out(clk, rst_n);

beat_t beat_q [NUM_STAGES];
logic  valid_q[NUM_STAGES];

(* max_fanout = 64 *) logic enable;
assign enable   = (!valid_q[NUM_STAGES - 1] || skid_in.ready) && !table_write_valid;
assign in.ready = enable;

always_ff @(posedge clk) begin
    if (rst_n == 1'b0) begin
        for (int S = 0; S < NUM_STAGES; S++) begin
            valid_q[S] <= 1'b0;
        end
    end else begin
        if (enable) begin
            valid_q[0] <= in.valid;
            for (int S = 1; S < NUM_STAGES; S++) begin
                valid_q[S] <= valid_q[S - 1];
            end
        end
    end
end

always_ff @(posedge clk) begin
    if (enable) begin
        beat_q[0].data      <= in.data;
        beat_q[0].keep      <= in.keep;
        beat_q[0].typ       <= in.typ;
        beat_q[0].last      <= in.last;
        beat_q[0].pass      <= pass;
        beat_q[0].load_sel  <= conf.ctrl.load_sel;
        beat_q[0].store_sel <= conf.ctrl.store_sel;
        beat_q[0].emit      <= conf.ctrl.emit;
        beat_q[0].lookup    <= conf.ctrl.lookup;
        beat_q[0].bloom     <= conf.ctrl.bloom;
        beat_q[0].hash64    <= conf.ctrl.hash64;
        beat_q[0].shift     <= conf.ctrl.shift;
        beat_q[0].base      <= conf.ctrl.base[WORD_BITS - 1:0];
        beat_q[0].lut_min   <= conf.lut_min;
        for (int S = 1; S < NUM_STAGES; S++) begin
            beat_q[S] <= beat_q[S - 1];
        end
    end
end

// -- Stage R to R + 1: difference (R = RANGE_START) -----------------------------------------------
// One 32 bit subtract per lane. For INT64 the borrow of the even lane enters the odd lane, as the
// lane pairs of the FilterCompare do, so the pair holds the 64 bit difference.
beat_t diff_beat, shift_beat;
logic  is_64_diff, is_64_shift;

assign diff_beat   = beat_q[RANGE_START];
assign shift_beat  = beat_q[RANGE_START + 1];
assign is_64_diff  = GET_TYPE_WIDTH(diff_beat.typ) == 64;
assign is_64_shift = GET_TYPE_WIDTH(shift_beat.typ) == 64;

logic[NUM_32B_LANES - 1:0][31:0] diff, diff_q;

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    logic[31:0] value_even, value_odd, min_even, min_odd;
    logic       borrow_even;

    assign value_even = diff_beat.data[8 * I +: 4];
    assign value_odd  = diff_beat.data[8 * I + 4 +: 4];
    assign min_even   = diff_beat.lut_min[31:0];
    assign min_odd    = is_64_diff ? diff_beat.lut_min[63:32] : diff_beat.lut_min[31:0];

    assign {borrow_even, diff[2 * I]} = {1'b0, value_even} + {1'b0, ~min_even} + 33'd1;
    assign diff[2 * I + 1]            = value_odd + ~min_odd + 32'(!is_64_diff || borrow_even);
end

always_ff @(posedge clk) begin
    if (enable) begin
        diff_q <= diff;
    end
end

// -- Stage R + 1 to R + 2: shift ------------------------------------------------------------------
logic[NUM_32B_LANES - 1:0][INDEX_BITS - 1:0] offset_q;
logic[4:0]                                   shift_1;

assign shift_1 = shift_beat.shift[4:0];

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    logic[WIDE_BITS - 1:0] wide_odd;

    assign wide_odd = is_64_shift ? {diff_q[2 * I + 1][WIDE_BITS - 33:0], diff_q[2 * I]} : WIDE_BITS'(diff_q[2 * I + 1]);

    always_ff @(posedge clk) begin
        if (enable) begin
            offset_q[2 * I]     <= INDEX_BITS'(diff_q[2 * I] >> shift_1);
            offset_q[2 * I + 1] <= INDEX_BITS'(wide_odd >> shift_1);
        end
    end
end

// -- Stage R + 2 to R + 3: bit index --------------------------------------------------------------
// The word of the bit and its position in the word, four times for the four positions of a read
logic[NUM_32B_LANES - 1:0][INDEX_BITS - 1:0] index_q;
logic[NUM_32B_LANES - 1:0][WORD_BITS - 1:0]  index_addr;
logic[NUM_32B_LANES - 1:0][3:0][5:0]         index_pos;

always_ff @(posedge clk) begin
    if (enable) begin
        for (int I = 0; I < NUM_32B_LANES; I++) begin
            index_q[I] <= offset_q[I] + {beat_q[RANGE_START + 2].base, 6'b0};
        end
    end
end

for (genvar I = 0; I < NUM_32B_LANES; I++) begin
    assign index_addr[I] = index_q[I][INDEX_BITS - 1:6];
    assign index_pos[I]  = {4{index_q[I][5:0]}};
end

// -- Table read -----------------------------------------------------------------------------------
// The word of every lane and the four bits in it, at stage INDEX_STAGES. A bitmap reads its one bit
// four times.
logic[NUM_32B_LANES - 1:0][WORD_BITS - 1:0] read_addr;
logic[NUM_32B_LANES - 1:0][3:0][5:0]        read_pos;
logic[NUM_32B_LANES - 1:0]                  bits;

if (FILTER_BLOOM) begin : gen_bloom
    // Stage 0 to HASH_STAGES: the hash of every value, next to the bitmap index
    logic[NUM_32B_LANES - 1:0][63:0] hash;
    beat_t                           hash_beat;
    logic[WORD_BITS - 1:0]           mask;

    FilterHash #(
        .NUM_LANES(NUM_32B_LANES)
    ) inst_hash (
        .clk(clk),
        .enable(enable),

        .lanes(beat_q[0].data),
        .is_64(GET_TYPE_WIDTH(beat_q[0].typ) == 64),
        .hash64(beat_q[0].hash64),
        .hash(hash)
    );

    // Stage HASH_STAGES to INDEX_STAGES: the word of the Bloom filter, or the bitmap index
    assign hash_beat = beat_q[HASH_STAGES];
    assign mask      = ~({WORD_BITS{1'b1}} << hash_beat.shift);

    always_ff @(posedge clk) begin
        if (enable) begin
            for (int I = 0; I < NUM_32B_LANES; I++) begin
                if (hash_beat.bloom) begin
                    read_addr[I] <= hash_beat.base + (hash[I][WORD_BITS - 1:0] & mask);
                    read_pos[I]  <= {hash[I][61:56], hash[I][53:48], hash[I][45:40], hash[I][37:32]};
                end else begin
                    read_addr[I] <= index_addr[I];
                    read_pos[I]  <= index_pos[I];
                end
            end
        end
    end
end else begin : gen_no_bloom
    assign read_addr = index_addr;
    assign read_pos  = index_pos;
end

FilterTable #(
    .NUM_LANES(NUM_32B_LANES)
) inst_table (
    .clk(clk),

    .table_write_valid(table_write_valid),
    .table_write(table_write),

    .read_enable(enable),
    .read_addr(read_addr),
    .read_pos(read_pos),
    .bits(bits)
);

// -- Output ---------------------------------------------------------------------------------------
// The bits of the table belong to the beat in the last stage. A 64 bit value has its bit in the odd
// lane of its pair.
logic[NUM_32B_LANES - 1:0] hit;
logic                      is_64_out;
beat_t                     last_beat;

assign last_beat = beat_q[NUM_STAGES - 1];
assign is_64_out = GET_TYPE_WIDTH(last_beat.typ) == 64;

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    assign hit[I] = is_64_out ? bits[2 * I + 1] : bits[I];
end
for (genvar I = NUM_64B_LANES; I < NUM_32B_LANES; I++) begin
    assign hit[I] = is_64_out ? 1'b0 : bits[I];
end

always_comb begin
    skid_in.data      = last_beat;
    skid_in.data.pass = last_beat.pass & (last_beat.lookup ? hit : '1);
end

assign skid_in.valid = valid_q[NUM_STAGES - 1] && !table_write_valid;

SkidBuffer #(
    .data_t(beat_t)
) inst_skid_buffer (
    .clk(clk),
    .rst_n(rst_n),

    .in(skid_in),
    .out(skid_out)
);

assign out.data       = skid_out.data.data;
assign out.keep       = skid_out.data.keep;
assign out.typ        = skid_out.data.typ;
assign out.last       = skid_out.data.last;
assign out.valid      = skid_out.valid;
assign skid_out.ready = out.ready;

assign out_load_sel  = skid_out.data.load_sel;
assign out_store_sel = skid_out.data.store_sel;
assign out_emit      = skid_out.data.emit;
assign out_pass      = skid_out.data.pass;

endmodule

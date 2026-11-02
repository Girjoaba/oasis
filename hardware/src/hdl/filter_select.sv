`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * Turns the pass bits of the FilterCompare into the selection of the values of a beat and carries
 * the selection of a row group across its column chunks in a bitmap of SEL_ROWS bits.
 *
 * The columns of one row group stream through the stage one after the other, each as one chunk
 * that ends with last. A row counter gives every value its row in the row group. With load_sel the
 * bitmap bits of the rows are ANDed into the pass bits, with store_sel the result is written back.
 * The first filter column of a row group stores without loading, which overwrites the bitmap of the
 * previous row group. Without emit no value is selected, so the chunk leaves no output.
 *
 * Lanes: 32 bit values (INT32_T, FLOAT_T) fill 16 rows per beat and use pass[15:0], 64 bit values
 * (INT64_T, DOUBLE_T) 8 rows and pass[7:0]. select has one bit per 32 bit lane, so a 64 bit value
 * sets two bits. Other types pass through unselected (select all ones with emit) and may not load or
 * store. The bitmap holds 16 rows per word in two halves, so a 64 bit beat touches one half.
 *
 * One register stage. The bitmap read of a beat is issued when it enters the stage and arrives with
 * it in the register, where the write happens as it leaves. A row is touched once per chunk, but
 * under backpressure the first beat of a chunk enters on the edge where the last beat of the
 * previous chunk leaves. In a row group of at most 16 rows both beats hit the same word, so the
 * RAMs forward a write to the read of the same edge.
 */
module FilterSelect
    import libstf::*;
#(
    parameter DATABEAT_SIZE,
    parameter SEL_ROWS
) (
    input logic clk,
    input logic rst_n,

    input logic load_sel,  // Valid with in.valid
    input logic store_sel, // Valid with in.valid
    input logic emit,      // Valid with in.valid

    input logic[DATABEAT_SIZE / 4 - 1:0] pass, // One bit per value of in, valid with in.valid

    typed_ndata_i.s in,                           // #(DATABEAT_SIZE)
    typed_ndata_i.m out,                          // #(DATABEAT_SIZE)
    output logic[DATABEAT_SIZE / 4 - 1:0] select  // One bit per 32 bit lane of out, valid with out.valid
);

localparam NUM_32B_LANES = DATABEAT_SIZE / 4;
localparam NUM_64B_LANES = DATABEAT_SIZE / 8;
localparam ROW_BITS      = $clog2(SEL_ROWS);
localparam WORD_BITS     = ROW_BITS - 4; // 16 rows per bitmap word

`ASSERT_ELAB(DATABEAT_SIZE == 64)
`ASSERT_ELAB(SEL_ROWS == 2 ** ROW_BITS && SEL_ROWS >= 32)

// -- Input stage: row counter and bitmap read -----------------------------------------------------
logic is_32, is_64;
assign is_32 = GET_TYPE_WIDTH(in.typ) == 32;
assign is_64 = GET_TYPE_WIDTH(in.typ) == 64;

logic advance;
assign in.ready = advance;

logic[ROW_BITS - 1:0] row;

always_ff @(posedge clk) begin
    if (rst_n == 1'b0) begin
        row <= '0;
    end else begin
        if (in.valid && in.ready) begin
            if (in.last) begin
                row <= '0;
            end else begin
                row <= row + (is_64 ? NUM_64B_LANES : NUM_32B_LANES);
            end
        end
    end
end

`ifndef SYNTHESIS
assert property (@(posedge clk) disable iff (!rst_n)
    !(in.valid && (load_sel || store_sel)) || is_32 || is_64)
else $fatal(1, "FilterSelect cannot select values of type %0d", in.typ);

assert property (@(posedge clk) disable iff (!rst_n)
    !(in.valid && (load_sel || store_sel)) || row + NUM_32B_LANES <= SEL_ROWS)
else $fatal(1, "FilterSelect row group exceeds the %0d rows of the selection bitmap", SEL_ROWS);
`endif

// -- Register stage -------------------------------------------------------------------------------
data8_t[DATABEAT_SIZE - 1:0] data;
logic  [DATABEAT_SIZE - 1:0] keep;
type_t                       typ;
logic                        last;
logic  [NUM_32B_LANES - 1:0] pass_q;
logic  [ROW_BITS - 1:0]      row_q;
logic                        load_q, store_q, emit_q;
logic                        valid;

assign advance = !valid || out.ready;

always_ff @(posedge clk) begin
    if (rst_n == 1'b0) begin
        data    <= 'X;
        keep    <= 'X;
        typ     <= type_t'('X);
        last    <= 'X;
        pass_q  <= 'X;
        row_q   <= 'X;
        load_q  <= 'X;
        store_q <= 'X;
        emit_q  <= 'X;
        valid   <= 1'b0;
    end else begin
        if (advance) begin
            data    <= in.data;
            keep    <= in.keep;
            typ     <= in.typ;
            last    <= in.last;
            pass_q  <= pass;
            row_q   <= row;
            load_q  <= load_sel;
            store_q <= store_sel;
            emit_q  <= emit;
            valid   <= in.valid;
        end
    end
end

logic is_32_q, is_64_q;
assign is_32_q = GET_TYPE_WIDTH(typ) == 32;
assign is_64_q = GET_TYPE_WIDTH(typ) == 64;

assign out.data  = data;
assign out.keep  = keep;
assign out.typ   = typ;
assign out.last  = last;
assign out.valid = valid;

// -- Selection bitmap -----------------------------------------------------------------------------
// Two RAMs hold the two halves of a bitmap word: the rows 0 to 7 and 8 to 15 of the word.
logic[NUM_64B_LANES - 1:0] bits_lo, bits_hi;
logic[NUM_64B_LANES - 1:0] write_lo, write_hi;
logic                      write_en_lo, write_en_hi;

ReadyRAM #(
    .DATA_WIDTH(NUM_64B_LANES),
    .ADDR_WIDTH(WORD_BITS),
    .READ_DURING_WRITE(1)
) inst_bitmap_lo (
    .clk(clk),

    .write_addr(row_q[ROW_BITS - 1:4]),
    .write_data(write_lo),
    .write_enable(write_en_lo),

    .read_addr(row[ROW_BITS - 1:4]),
    .read_data(bits_lo),
    .read_ready(advance)
);

ReadyRAM #(
    .DATA_WIDTH(NUM_64B_LANES),
    .ADDR_WIDTH(WORD_BITS),
    .READ_DURING_WRITE(1)
) inst_bitmap_hi (
    .clk(clk),

    .write_addr(row_q[ROW_BITS - 1:4]),
    .write_data(write_hi),
    .write_enable(write_en_hi),

    .read_addr(row[ROW_BITS - 1:4]),
    .read_data(bits_hi),
    .read_ready(advance)
);

// -- Select ---------------------------------------------------------------------------------------
logic[NUM_64B_LANES - 1:0] sel_64;
logic[NUM_32B_LANES - 1:0] sel_32, sel_64_lanes;

assign sel_64 = pass_q[NUM_64B_LANES - 1:0] & (load_q ? (row_q[3] ? bits_hi : bits_lo) : '1);
assign sel_32 = pass_q                      & (load_q ? {bits_hi, bits_lo}             : '1);

for (genvar I = 0; I < NUM_64B_LANES; I++) begin
    assign sel_64_lanes[2 * I]     = sel_64[I];
    assign sel_64_lanes[2 * I + 1] = sel_64[I];
end

always_comb begin
    if (!emit_q) begin
        select = '0;
    end else if (is_64_q) begin
        select = sel_64_lanes;
    end else if (is_32_q) begin
        select = sel_32;
    end else begin
        select = '1;
    end
end

// The selection before the emit gate is stored, so a chunk without emit stores the same bits.
assign write_en_lo = valid && out.ready && store_q && (is_32_q || (is_64_q && !row_q[3]));
assign write_en_hi = valid && out.ready && store_q && (is_32_q || (is_64_q &&  row_q[3]));
assign write_lo    = is_64_q ? sel_64 : sel_32[NUM_64B_LANES - 1:0];
assign write_hi    = is_64_q ? sel_64 : sel_32[NUM_32B_LANES - 1:NUM_64B_LANES];

endmodule

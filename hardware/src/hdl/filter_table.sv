`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * The lookup table of one FilterStage: FILTER_TABLE_BITS bits in 64 bit words, read one bit per
 * lane and cycle. Every lane pair 2I, 2I + 1 reads its own copy of the table through the two ports
 * of a true dual-port URAM, so the table exists NUM_LANES / 2 times. A write goes to all copies
 * through port A, in a cycle without read.
 *
 * The read of a lane is the bit index read_bit[I]; its bit arrives in bits[I] FILTER_TABLE_LATENCY
 * enabled cycles later. All registers move only with read_enable, so the reads stall with the
 * pipeline of the FilterLookup and the bits wait in the output registers. The URAM read is followed
 * by two registers for the cascade of the URAMs of one copy, then the bit select of the word.
 */
module FilterTable
    import libstf::*;
    import oasis::*;
#(
    parameter NUM_LANES
) (
    input logic clk,

    input logic                table_write_valid,
    input filter_table_write_t table_write,

    input  logic                                                  read_enable,
    input  logic[NUM_LANES - 1:0][FILTER_TABLE_WORD_BITS + 5:0] read_bit,
    output logic[NUM_LANES - 1:0]                                 bits
);

localparam NUM_COPIES = NUM_LANES / 2;
localparam WORDS      = FILTER_TABLE_WORDS;
localparam WORD_BITS  = FILTER_TABLE_WORD_BITS;

`ASSERT_ELAB(NUM_LANES % 2 == 0)
`ASSERT_ELAB(WORDS == 2 ** WORD_BITS)

`ifndef SYNTHESIS
assert property (@(posedge clk) table_write_valid !== 1'b1 || read_enable !== 1'b1)
else $fatal(1, "FilterTable read and write in the same cycle");
`endif

// The bit of the word, carried along with the read of the word
logic[NUM_LANES - 1:0][5:0] offset_q[3];

always_ff @(posedge clk) begin
    if (read_enable) begin
        for (int I = 0; I < NUM_LANES; I++) begin
            offset_q[0][I] <= read_bit[I][5:0];
        end
        offset_q[1] <= offset_q[0];
        offset_q[2] <= offset_q[1];
    end
end

for (genvar I = 0; I < NUM_COPIES; I++) begin : gen_copies
    (* ram_style = "ultra" *) data64_t ram[WORDS];

    logic[WORD_BITS - 1:0] addr_a, addr_b;
    data64_t               word_a, word_b;
    data64_t               word_a_q[2], word_b_q[2];
    logic                  bit_a, bit_b;

    assign addr_a = table_write_valid ? table_write.addr : read_bit[2 * I][WORD_BITS + 5:6];
    assign addr_b = read_bit[2 * I + 1][WORD_BITS + 5:6];

    // The ports in the shape of the UltraRAM template of Vivado (no change on write)
    logic enable_a;
    assign enable_a = table_write_valid || read_enable;

    always_ff @(posedge clk) begin
        if (enable_a) begin
            if (table_write_valid) begin
                ram[addr_a] <= table_write.data;
            end else begin
                word_a <= ram[addr_a];
            end
        end
    end

    always_ff @(posedge clk) begin
        if (read_enable) begin
            word_b <= ram[addr_b];
        end
    end

    always_ff @(posedge clk) begin
        if (read_enable) begin
            word_a_q[0] <= word_a;
            word_a_q[1] <= word_a_q[0];
            word_b_q[0] <= word_b;
            word_b_q[1] <= word_b_q[0];

            bit_a <= word_a_q[1][offset_q[2][2 * I]];
            bit_b <= word_b_q[1][offset_q[2][2 * I + 1]];
        end
    end

    assign bits[2 * I]     = bit_a;
    assign bits[2 * I + 1] = bit_b;
end

endmodule

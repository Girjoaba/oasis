`timescale 1ns / 1ps

`include "libstf_macros.svh"

/**
 * The MurmurHash64 of every value of a beat, the hash of the Bloom filters of DuckDB:
 *
 *   x ^= x >> 32; x *= C; x ^= x >> 32; x *= C; x ^= x >> 32;   C = 0xd6e8feb86659fd93
 *
 * One hash unit per 32 bit lane. A 32 bit value is hashed as its zero extension, as DuckDB hashes
 * an INTEGER key, or with hash64 as its sign extension, as DuckDB hashes the BIGINT key of a chunk
 * that the decoder narrowed to 32 bits. With is_64 the lane pair 2I, 2I + 1 holds one 64 bit value,
 * which the odd lane of the pair hashes; the hash of the even lane is then of no use.
 *
 * Pipeline: the first xor, the first multiply with FILTER_HASH_MUL_STAGES registers after it, the
 * second xor, the second multiply with the same registers, the last xor; FILTER_HASH_LATENCY
 * enabled cycles from the input to hash. The registers after a multiply let Vivado split it over
 * the DSPs. All registers move with enable.
 */
module FilterHash
    import libstf::*;
    import oasis::*;
#(
    parameter NUM_LANES
) (
    input logic clk,
    input logic enable,

    input  logic[NUM_LANES - 1:0][31:0] lanes,
    input  logic                        is_64,
    input  logic                        hash64,
    output logic[NUM_LANES - 1:0][63:0] hash // FILTER_HASH_LATENCY enabled cycles after lanes
);

localparam logic[63:0] C          = 64'hd6e8feb86659fd93;
localparam             MUL_STAGES = FILTER_HASH_MUL_STAGES;

`ASSERT_ELAB(NUM_LANES % 2 == 0)
`ASSERT_ELAB(MUL_STAGES >= 1)
`ASSERT_ELAB(FILTER_HASH_LATENCY == 2 * MUL_STAGES + 3)

for (genvar I = 0; I < NUM_LANES; I++) begin : gen_units
    logic[63:0] key;
    logic[63:0] x0_q, x1_q, x2_q;
    logic[63:0] p0_q[MUL_STAGES], p1_q[MUL_STAGES];

    if (I % 2 == 1) begin : gen_odd
        assign key = is_64 ? {lanes[I], lanes[I - 1]} : {{32{hash64 && lanes[I][31]}}, lanes[I]};
    end else begin : gen_even
        assign key = {{32{hash64 && lanes[I][31]}}, lanes[I]};
    end

    always_ff @(posedge clk) begin
        if (enable) begin
            x0_q <= key ^ (key >> 32);

            p0_q[0] <= x0_q * C;
            for (int S = 1; S < MUL_STAGES; S++) begin
                p0_q[S] <= p0_q[S - 1];
            end

            x1_q <= p0_q[MUL_STAGES - 1] ^ (p0_q[MUL_STAGES - 1] >> 32);

            p1_q[0] <= x1_q * C;
            for (int S = 1; S < MUL_STAGES; S++) begin
                p1_q[S] <= p1_q[S - 1];
            end

            x2_q <= p1_q[MUL_STAGES - 1] ^ (p1_q[MUL_STAGES - 1] >> 32);
        end
    end

    assign hash[I] = x2_q;
end

endmodule

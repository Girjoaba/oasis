/**
 * This file is part of the Coyote <https://github.com/fpgasystems/Coyote>
 *
 * MIT Licence
 * Copyright (c) 2021-2025, Systems Group, ETH Zurich
 * All rights reserved.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:

 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.

 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

`timescale 1ns / 1ps

import lynxTypes::*;

`include "axi_macros.svh"
`include "lynx_macros.svh"

/**
 * User logic
 * 
 */
module design_user_logic_c0_0 (
    // AXI4L CONTROL
    AXI4L.s                     axi_ctrl,

    // NOTIFY
    metaIntf.m                  notify,

    // DESCRIPTORS
    metaIntf.m                  sq_rd, 
    metaIntf.m                  sq_wr,
    metaIntf.s                  cq_rd,
    metaIntf.s                  cq_wr,
    metaIntf.s                  rq_rd,
    metaIntf.s                  rq_wr,

    // HOST DATA STREAMS
    AXI4SR.s                    axis_host_recv [N_STRM_AXI],
    AXI4SR.m                    axis_host_send [N_STRM_AXI],

    // RDMA DATA STREAMS REQUESTER
    AXI4SR.s                    axis_rreq_recv [N_RDMA_AXI],
    AXI4SR.m                    axis_rreq_send [N_RDMA_AXI],

    // RDMA DATA STREAMS RESPONDER
    AXI4SR.s                    axis_rrsp_recv [N_RDMA_AXI],
    AXI4SR.m                    axis_rrsp_send [N_RDMA_AXI],



    // Clock and reset
    input  wire                 aclk,
    input  wire[0:0]            aresetn
);

/* -- Tie-off unused interfaces and signals ----------------------------- */
//always_comb axi_ctrl.tie_off_s();
//always_comb notify.tie_off_m();
//always_comb sq_rd.tie_off_m();
//always_comb sq_wr.tie_off_m();
//always_comb cq_rd.tie_off_s();
//always_comb cq_wr.tie_off_s();
//always_comb rq_rd.tie_off_s();
//always_comb rq_wr.tie_off_s();
//always_comb axis_host_recv[0].tie_off_s();
//always_comb axis_host_send[0].tie_off_m();
//always_comb axis_host_recv[1].tie_off_s();
//always_comb axis_host_send[1].tie_off_m();
//always_comb axis_rreq_recv[0].tie_off_s();
//always_comb axis_rreq_send[0].tie_off_m();
//always_comb axis_rrsp_recv[0].tie_off_s();
//always_comb axis_rrsp_send[0].tie_off_m();
//always_comb axis_rreq_recv[1].tie_off_s();
//always_comb axis_rreq_send[1].tie_off_m();
//always_comb axis_rrsp_recv[1].tie_off_s();
//always_comb axis_rrsp_send[1].tie_off_m();

/* ---------------------------------------------------------------------- */
// USER LOGIC 
/* ---------------------------------------------------------------------- */
`include "vfpga_top.svh"

endmodule
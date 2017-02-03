#!/usr/bin/env python
"""Test the bpod module"""
from __future__ import division
from future.builtins import zip
from future.builtins import range
import unittest
import copy
import os
from os.path import join
from shutil import rmtree

import numpy as np

import modred.parallel as parallel
from modred.bpod import *
from modred.vectorspace import *
from modred import util
from modred import vectors as V


def get_system_mats(num_states, num_inputs, num_outputs):
    eig_vals = 0.05 * np.random.random(num_states) + 0.8
    eig_vecs = 2. * np.random.random((num_states, num_states)) - 1.
    A = np.mat(
        np.linalg.inv(eig_vecs).dot(np.diag(eig_vals)).dot(eig_vecs))
    B = np.mat(2. * np.random.random((num_states, num_inputs)) - 1.)
    C = np.mat(2. * np.random.random((num_outputs, num_states)) - 1.)
    return A, B, C


def get_direct_impulse_response_mats(A, B, num_steps):
    num_states, num_inputs = B.shape
    direct_vecs_mat = np.mat(np.zeros((num_states, num_steps * num_inputs)))
    A_powers = np.mat(np.identity(num_states))
    for idx in xrange(num_steps):
        direct_vecs_mat[:, idx * num_inputs:(idx + 1) * num_inputs] =\
            A_powers * B
        A_powers = A_powers * A
    return direct_vecs_mat


def get_adjoint_impulse_response_mats(A, C, num_steps, weights_mat):
    num_outputs, num_states = C.shape
    A_adjoint = np.linalg.inv(weights_mat) * A.H * weights_mat
    C_adjoint = np.linalg.inv(weights_mat) * C.H
    adjoint_vecs_mat = np.mat(np.zeros((num_states, num_steps * num_outputs)))
    A_adjoint_powers = np.mat(np.identity(num_states))
    for idx in xrange(num_steps):
        adjoint_vecs_mat[:, (idx * num_outputs):(idx + 1) * num_outputs] =\
            A_adjoint_powers * C_adjoint
        A_adjoint_powers = A_adjoint_powers * A_adjoint
    return adjoint_vecs_mat


#@unittest.skip('Testing something else.')
@unittest.skipIf(parallel.is_distributed(), 'Serial only.')
class TestBPODMatrices(unittest.TestCase):
    def setUp(self):
        self.num_states = 10
        self.num_steps = self.num_states + 1


    def test_all(self):
        # Set test tolerances.  Separate, more relaxed tolerances are required
        # for testing the BPOD modes, since that test requires "squaring" the
        # gramians and thus involves more ill-conditioned matrices.
        rtol = 1e-8
        atol = 1e-10
        rtol_sqr = 1e-8
        atol_sqr = 1e-8

        # Generate weights to test different inner products.  Keep most of the
        # weights close to one, to avoid overly weighting certain states over
        # others.  This can dramatically affect the rate at which the tests
        # pass.
        ws = np.identity(self.num_states)
        ws[0, 0] = 1.01
        ws[2, 1] = 0.02
        ws[1, 2] = 0.02
        weights_list = [None, 0.02 * np.random.random(self.num_states) + 1., ws]
        weights_mats = [
            np.mat(np.identity(self.num_states)),
            np.mat(np.diag(weights_list[1])),
            np.mat(ws)]

        # Check different system sizes.  Make sure to test a single input/output
        # in addition to multiple inputs/outputs.  Also allow for the number of
        # inputs/outputs to exceed the number of states.
        for num_inputs in [1, np.random.randint(2, high=self.num_states + 2)]:

            for num_outputs in [
                1, np.random.randint(2, high=self.num_states + 2)]:

                # Get state space system
                A, B, C = get_system_mats(
                    self.num_states, num_inputs, num_outputs)

                # Compute direct impulse response
                direct_vecs_mat = get_direct_impulse_response_mats(
                    A, B, self.num_steps)

                # Loop through different inner product weights
                for weights, weights_mat in zip(weights_list, weights_mats):

                    # Define inner product based on weights
                    IP = VectorSpaceMatrices(
                        weights=weights).compute_inner_product_mat

                    # Compute adjoint impulse response
                    adjoint_vecs_mat = get_adjoint_impulse_response_mats(
                        A, C, self.num_steps, weights_mat)

                    # Compute BPOD using modred.  Use absolute tolerance to
                    # avoid Hankel singular values that approach numerical
                    # precision.  Use relative tolerance to avoid Hankel
                    # singular values which may correspond to very
                    # uncontrollable/unobservable states.  It is ok to use a
                    # more relaxed tolerance here than in the actual test/assert
                    # statements, as here we are saying it is ok to ignore
                    # highly uncontrollable/unobservable states, rather than
                    # allowing loose tolerances in the comparison of two
                    # numbers.  Furthermore, it is likely that in actual use,
                    # users would want to ignore relatively small Hankel
                    # singular values anyway, as that is the point of doing a
                    # balancing transformation.
                    (direct_modes_mat, adjoint_modes_mat, sing_vals,
                    L_sing_vecs, R_sing_vecs, Hankel_mat) =\
                    compute_BPOD_matrices(
                        direct_vecs_mat, adjoint_vecs_mat,
                        num_inputs=num_inputs, num_outputs=num_outputs,
                        inner_product_weights=weights, rtol=1e-6, atol=1e-12,
                        return_all=True)

                    # Check Hankel mat values.  These are computed fast
                    # internally by only computing the first column and last row
                    # of chunks.  Here, simply take all the inner products.
                    Hankel_mat_slow = IP(adjoint_vecs_mat, direct_vecs_mat)
                    np.testing.assert_allclose(
                        Hankel_mat, Hankel_mat_slow, rtol=rtol, atol=atol)

                    # Check properties of SVD of Hankel matrix.  Since the SVD
                    # may be truncated, instead of checking orthogonality and
                    # reconstruction of the Hankel matrix, check that the left
                    # and right singular vectors satisfy eigendecomposition
                    # properties with respect to the Hankel matrix.  Since this
                    # involves "squaring" the Hankel matrix, it requires more
                    # relaxed test tolerances.
                    np.testing.assert_allclose(
                        Hankel_mat * Hankel_mat.T * L_sing_vecs,
                        L_sing_vecs * np.mat(np.diag(sing_vals ** 2.)),
                        rtol=rtol_sqr, atol=atol_sqr)
                    np.testing.assert_allclose(
                        Hankel_mat.T * Hankel_mat * R_sing_vecs,
                        R_sing_vecs * np.mat(np.diag(sing_vals ** 2.)),
                        rtol=rtol_sqr, atol=atol_sqr)

                    # Check that the modes diagonalize the gramians.  This test
                    # requires looser tolerances than the other tests, likely
                    # due to the "squaring" of the matrices in computing the
                    # gramians.
                    np.testing.assert_allclose((
                        IP(adjoint_modes_mat, direct_vecs_mat) *
                        IP(direct_vecs_mat, adjoint_modes_mat)),
                        np.diag(sing_vals),
                        rtol=rtol_sqr, atol=atol_sqr)
                    np.testing.assert_allclose((
                        IP(direct_modes_mat, adjoint_vecs_mat) *
                        IP(adjoint_vecs_mat, direct_modes_mat)),
                        np.diag(sing_vals),
                        rtol=rtol_sqr, atol=atol_sqr)

                    # Check that if mode indices are passed in, the correct
                    # modes are returned.
                    mode_indices = np.unique(np.random.randint(
                        0, high=sing_vals.size, size=(sing_vals.size // 2)))
                    direct_modes_mat_sliced, adjoint_modes_mat_sliced =\
                    compute_BPOD_matrices(
                        direct_vecs_mat, adjoint_vecs_mat,
                        direct_mode_indices=mode_indices,
                        adjoint_mode_indices=mode_indices,
                        num_inputs=num_inputs, num_outputs=num_outputs,
                        inner_product_weights=weights,
                        rtol=1e-12, atol=1e-12, return_all=True)[:2]
                    np.testing.assert_allclose(
                        direct_modes_mat_sliced,
                        direct_modes_mat[:, mode_indices],
                        rtol=rtol, atol=atol)
                    np.testing.assert_allclose(
                        adjoint_modes_mat_sliced,
                        adjoint_modes_mat[:, mode_indices],
                        rtol=rtol, atol=atol)


#@unittest.skip('Testing something else.')
class TestBPODHandles(unittest.TestCase):
    """Test the BPOD class methods """
    def setUp(self):
        # Specify output locations
        if not os.access('.', os.W_OK):
            raise RuntimeError('Cannot write to current directory')
        self.test_dir = 'BPOD_files'
        if not os.path.isdir(self.test_dir):
            parallel.call_from_rank_zero(os.mkdir, self.test_dir)
        self.direct_vec_path = join(self.test_dir, 'direct_vec_%03d.txt')
        self.adjoint_vec_path = join(self.test_dir, 'adjoint_vec_%03d.txt')
        self.direct_mode_path = join(self.test_dir, 'direct_mode_%03d.txt')
        self.adjoint_mode_path = join(self.test_dir, 'adjoint_mode_%03d.txt')

        # Specify system dimensions.  Test single inputs/outputs as well as
        # multiple inputs/outputs.  Also allow for more inputs/outputs than
        # states.
        self.num_states = 10
        self.num_inputs_list = [
            1,
            parallel.call_and_bcast(np.random.randint, 2, self.num_states + 2)]
        self.num_outputs_list = [
            1,
            parallel.call_and_bcast(np.random.randint, 2, self.num_states + 2)]

        # Specify how long to run impulse responses
        self.num_steps = self.num_states + 1

        parallel.barrier()


    def tearDown(self):
        parallel.barrier()
        parallel.call_from_rank_zero(rmtree, self.test_dir, ignore_errors=True)
        parallel.barrier()


    #@unittest.skip('Testing something else.')
    def test_init(self):
        """Test arguments passed to the constructor are assigned properly"""

        def my_load(fname): pass
        def my_save(data, fname): pass
        def my_IP(vec1, vec2): pass

        data_members_default = {'put_mat': util.save_array_text, 'get_mat':
             util.load_array_text,
            'verbosity': 0, 'L_sing_vecs': None, 'R_sing_vecs': None,
            'sing_vals': None, 'direct_vec_handles': None,
            'adjoint_vec_handles': None,
            'direct_vec_handles': None, 'adjoint_vec_handles': None,
            'Hankel_mat': None,
            'vec_space': VectorSpaceHandles(inner_product=my_IP, verbosity=0)}

        # Get default data member values
        #self.maxDiff = None
        for k,v in util.get_data_members(
            BPODHandles(my_IP, verbosity=0)).items():
            self.assertEqual(v, data_members_default[k])

        my_BPOD = BPODHandles(my_IP, verbosity=0)
        data_members_modified = copy.deepcopy(data_members_default)
        data_members_modified['vec_space'] = VectorSpaceHandles(
            inner_product=my_IP, verbosity=0)
        for k,v in util.get_data_members(my_BPOD).items():
            self.assertEqual(v, data_members_modified[k])

        my_BPOD = BPODHandles(my_IP, get_mat=my_load, verbosity=0)
        data_members_modified = copy.deepcopy(data_members_default)
        data_members_modified['get_mat'] = my_load
        for k,v in util.get_data_members(my_BPOD).items():
            self.assertEqual(v, data_members_modified[k])

        my_BPOD = BPODHandles(my_IP, put_mat=my_save, verbosity=0)
        data_members_modified = copy.deepcopy(data_members_default)
        data_members_modified['put_mat'] = my_save
        for k,v in util.get_data_members(my_BPOD).items():
            self.assertEqual(v, data_members_modified[k])

        max_vecs_per_node = 500
        my_BPOD = BPODHandles(
            my_IP, max_vecs_per_node=max_vecs_per_node, verbosity=0)
        data_members_modified = copy.deepcopy(data_members_default)
        data_members_modified['vec_space'].max_vecs_per_node = \
            max_vecs_per_node
        data_members_modified['vec_space'].max_vecs_per_proc = \
            max_vecs_per_node * parallel.get_num_nodes() / parallel.\
            get_num_procs()
        for k,v in util.get_data_members(my_BPOD).items():
            self.assertEqual(v, data_members_modified[k])


    #@unittest.skip('Testing something else.')
    def test_puts_gets(self):
        """Test that put/get work in base class."""
        # Generate some random data
        Hankel_mat_true = parallel.call_and_bcast(
            np.random.random, ((self.num_states, self.num_states)))
        L_sing_vecs_true, sing_vals_true, R_sing_vecs_true = \
            parallel.call_and_bcast(util.svd, Hankel_mat_true)

        # Store the data in a BPOD object
        BPOD_save = BPODHandles(None, verbosity=0)
        BPOD_save.Hankel_mat = Hankel_mat_true
        BPOD_save.sing_vals = sing_vals_true
        BPOD_save.L_sing_vecs = L_sing_vecs_true
        BPOD_save.R_sing_vecs = R_sing_vecs_true

        # Use the BPOD object to save the data to disk
        sing_vals_path = join(self.test_dir, 'sing_vals.txt')
        L_sing_vecs_path = join(self.test_dir, 'L_sing_vecs.txt')
        R_sing_vecs_path = join(self.test_dir, 'R_sing_vecs.txt')
        Hankel_mat_path = join(self.test_dir, 'Hankel_mat.txt')
        BPOD_save.put_decomp(sing_vals_path, L_sing_vecs_path, R_sing_vecs_path)
        BPOD_save.put_Hankel_mat(Hankel_mat_path)
        parallel.barrier()

        # Create a BPOD object and use it to load the data from disk
        BPOD_load = BPODHandles(None, verbosity=0)
        BPOD_load.get_Hankel_mat(Hankel_mat_path)
        BPOD_load.get_decomp(sing_vals_path, L_sing_vecs_path, R_sing_vecs_path)

        # Compare loaded data or original data
        np.testing.assert_equal(BPOD_load.Hankel_mat, Hankel_mat_true)
        np.testing.assert_equal(BPOD_load.sing_vals, sing_vals_true)
        np.testing.assert_equal(BPOD_load.L_sing_vecs, L_sing_vecs_true)
        np.testing.assert_equal(BPOD_load.R_sing_vecs, R_sing_vecs_true)


    # Compute impulse responses and generate corresponding handles
    def _helper_get_impulse_response_handles(self, num_inputs, num_outputs):
        # Get state space system
        A, B, C = parallel.call_and_bcast(
            get_system_mats, self.num_states, num_inputs, num_outputs)

        # Run impulse responses
        direct_vec_mat = parallel.call_and_bcast(
            get_direct_impulse_response_mats, A, B, self.num_steps)
        adjoint_vec_mat = parallel.call_and_bcast(
            get_adjoint_impulse_response_mats, A, C, self.num_steps,
            np.identity(self.num_states))

        # Save data to disk
        direct_vec_handles = [
            V.VecHandleArrayText(self.direct_vec_path % i)
            for i in xrange(direct_vec_mat.shape[1])]
        adjoint_vec_handles = [
            V.VecHandleArrayText(self.adjoint_vec_path % i)
            for i in xrange(adjoint_vec_mat.shape[1])]
        if parallel.is_rank_zero():
            for idx, handle in enumerate(direct_vec_handles):
                handle.put(direct_vec_mat[:, idx])
            for idx, handle in enumerate(adjoint_vec_handles):
                handle.put(adjoint_vec_mat[:, idx])

        parallel.barrier()
        return direct_vec_handles, adjoint_vec_handles


    #@unittest.skip('Testing something else.')
    def test_compute_decomp(self):
        """Test that can take vecs, compute the Hankel and SVD matrices. """
        # Set test tolerances.  Separate, more relaxed tolerances may be
        # required for testing the SVD matrices, since that test requires
        # "squaring" the Hankel matrix and thus involves more ill-conditioned
        # matrices.
        rtol = 1e-8
        atol = 1e-10
        rtol_sqr = 1e-8
        atol_sqr = 1e-8

        # Test a single input/output as well as multiple inputs/outputs.  Allow
        # for more inputs/outputs than states.  (This is determined in setUp()).
        for num_inputs in self.num_inputs_list:
            for num_outputs in self.num_outputs_list:

                # Get impulse response data
                direct_vec_handles, adjoint_vec_handles =\
                self._helper_get_impulse_response_handles(
                    num_inputs, num_outputs)

                # Compute BPOD using modred.
                BPOD = BPODHandles(np.vdot, verbosity=0)
                sing_vals, L_sing_vecs, R_sing_vecs = BPOD.compute_decomp(
                    direct_vec_handles, adjoint_vec_handles,
                    num_inputs=num_inputs, num_outputs=num_outputs)

                # Check Hankel mat values.  These are computed fast
                # internally by only computing the first column and last row
                # of chunks.  Here, simply take all the inner products.
                Hankel_mat_slow = BPOD.vec_space.compute_inner_product_mat(
                    adjoint_vec_handles, direct_vec_handles)
                np.testing.assert_allclose(
                    BPOD.Hankel_mat, Hankel_mat_slow, rtol=rtol, atol=atol)

                # Check properties of SVD of Hankel matrix.  Since the SVD
                # may be truncated, instead of checking orthogonality and
                # reconstruction of the Hankel matrix, check that the left
                # and right singular vectors satisfy eigendecomposition
                # properties with respect to the Hankel matrix.  Since this
                # involves "squaring" the Hankel matrix, it may require more
                # relaxed test tolerances.
                np.testing.assert_allclose(
                    BPOD.Hankel_mat * BPOD.Hankel_mat.T * BPOD.L_sing_vecs,
                    BPOD.L_sing_vecs * np.mat(np.diag(BPOD.sing_vals ** 2.)),
                    rtol=rtol_sqr, atol=atol_sqr)
                np.testing.assert_allclose(
                    BPOD.Hankel_mat.T * BPOD.Hankel_mat * BPOD.R_sing_vecs,
                    BPOD.R_sing_vecs * np.mat(np.diag(BPOD.sing_vals ** 2.)),
                    rtol=rtol_sqr, atol=atol_sqr)

                # Check that returned values match internal values
                np.testing.assert_equal(sing_vals, BPOD.sing_vals)
                np.testing.assert_equal(L_sing_vecs, BPOD.L_sing_vecs)
                np.testing.assert_equal(R_sing_vecs, BPOD.R_sing_vecs)


    #@unittest.skip('Testing something else.')
    def test_compute_modes(self):
        """Test computing modes in serial and parallel."""
        # Set test tolerances.  More relaxed tolerances are required for testing
        # the BPOD modes, since that test requires "squaring" the gramians and
        # thus involves more ill-conditioned matrices.
        rtol_sqr = 1e-8
        atol_sqr = 1e-8

        # Test a single input/output as well as multiple inputs/outputs.  Allow
        # for more inputs/outputs than states.  (This is determined in setUp()).
        for num_inputs in self.num_inputs_list:
            for num_outputs in self.num_outputs_list:

                # Get impulse response data
                direct_vec_handles, adjoint_vec_handles =\
                    self._helper_get_impulse_response_handles(
                        num_inputs, num_outputs)

                # Create BPOD object and perform decomposition.  (The properties
                # defining a BPOD mode require manipulations involving the
                # correct decomposition, so we cannot isolate the mode
                # computation from the decomposition step.)  Use relative
                # tolerance to avoid Hankel singular values which may correspond
                # to very uncontrollable/unobservable states.  It is ok to use a
                # more relaxed tolerance here than in the actual test/assert
                # statements, as here we are saying it is ok to ignore highly
                # uncontrollable/unobservable states, rather than allowing loose
                # tolerances in the comparison of two numbers.  Furthermore, it
                # is likely that in actual use, users would want to ignore
                # relatively small Hankel singular values anyway, as that is the
                # point of doing a balancing transformation.
                BPOD = BPODHandles(np.vdot, verbosity=0)
                BPOD.compute_decomp(
                    direct_vec_handles, adjoint_vec_handles,
                    num_inputs=num_inputs, num_outputs=num_outputs,
                    rtol=1e-6, atol=1e-12)

                # Select a subset of modes to compute.  Compute at least half
                # the modes, and up to all of them.  Make sure to use unique
                # values.  (This may reduce the number of modes computed.)
                num_modes = parallel.call_and_bcast(
                    np.random.randint,
                    BPOD.sing_vals.size // 2, BPOD.sing_vals.size + 1)
                mode_idxs = np.unique(parallel.call_and_bcast(
                    np.random.randint,
                    0, BPOD.sing_vals.size, num_modes))

                # Create handles for the modes
                direct_mode_handles = [
                    V.VecHandleArrayText(self.direct_mode_path % i)
                    for i in mode_idxs]
                adjoint_mode_handles = [
                    V.VecHandleArrayText(self.adjoint_mode_path % i)
                    for i in mode_idxs]

                # Compute modes
                BPOD.compute_direct_modes(
                    mode_idxs, direct_mode_handles,
                    direct_vec_handles=direct_vec_handles)
                BPOD.compute_adjoint_modes(
                    mode_idxs, adjoint_mode_handles,
                    adjoint_vec_handles=adjoint_vec_handles)

                # Test modes against empirical gramians
                np.testing.assert_allclose(
                    BPOD.vec_space.compute_inner_product_mat(
                        adjoint_mode_handles, direct_vec_handles) *
                    BPOD.vec_space.compute_inner_product_mat(
                        direct_vec_handles, adjoint_mode_handles),
                    np.diag(BPOD.sing_vals[mode_idxs]),
                    rtol=rtol_sqr, atol=atol_sqr)
                np.testing.assert_allclose(
                    BPOD.vec_space.compute_inner_product_mat(
                        direct_mode_handles, adjoint_vec_handles) *
                    BPOD.vec_space.compute_inner_product_mat(
                        adjoint_vec_handles, direct_mode_handles),
                    np.diag(BPOD.sing_vals[mode_idxs]),
                    rtol=rtol_sqr, atol=atol_sqr)


    #@unittest.skip('Testing something else.')
    def test_compute_proj_coeffs(self):
        # Set test tolerances.  Use a slightly more relaxed absolute tolerance
        # here because the projection test uses modes that may correspond to
        # smaller Hankel singular values (i.e., less controllable/unobservable
        # states).  Those mode pairs are not as close to biorthogonal, so a more
        # relaxed tolerance is required.
        rtol = 1e-8
        atol = 1e-8

        # Test a single input/output as well as multiple inputs/outputs.  Allow
        # for more inputs/outputs than states.  (This is determined in setUp()).
        for num_inputs in self.num_inputs_list:
            for num_outputs in self.num_outputs_list:

                # Get impulse response data
                direct_vec_handles, adjoint_vec_handles =\
                self._helper_get_impulse_response_handles(
                    num_inputs, num_outputs)

                # Create BPOD object and compute decomposition, modes.  (The
                # properties defining a projection onto BPOD modes require
                # manipulations involving the correct decomposition and modes,
                # so we cannot isolate the projection step from those
                # computations.)  Use relative tolerance to avoid Hankel
                # singular values which may correspond to very
                # uncontrollable/unobservable states.  It is ok to use a
                # more relaxed tolerance here than in the actual test/assert
                # statements, as here we are saying it is ok to ignore
                # highly uncontrollable/unobservable states, rather than
                # allowing loose tolerances in the comparison of two
                # numbers.  Furthermore, it is likely that in actual use,
                # users would want to ignore relatively small Hankel
                # singular values anyway, as that is the point of doing a
                # balancing transformation.
                BPOD = BPODHandles(np.vdot, verbosity=0)
                BPOD.compute_decomp(
                    direct_vec_handles, adjoint_vec_handles,
                    num_inputs=num_inputs, num_outputs=num_outputs,
                    rtol=1e-6, atol=1e-12)
                mode_idxs = np.arange(BPOD.sing_vals.size)
                direct_mode_handles = [
                    V.VecHandleArrayText(self.direct_mode_path % i)
                    for i in mode_idxs]
                adjoint_mode_handles = [
                    V.VecHandleArrayText(self.adjoint_mode_path % i)
                    for i in mode_idxs]
                BPOD.compute_direct_modes(
                    mode_idxs, direct_mode_handles,
                    direct_vec_handles=direct_vec_handles)
                BPOD.compute_adjoint_modes(
                    mode_idxs, adjoint_mode_handles,
                    adjoint_vec_handles=adjoint_vec_handles)

                # Compute true projection coefficients by computing the inner
                # products between modes and snapshots.
                direct_proj_coeffs_true =\
                BPOD.vec_space.compute_inner_product_mat(
                    adjoint_mode_handles, direct_vec_handles)
                adjoint_proj_coeffs_true =\
                BPOD.vec_space.compute_inner_product_mat(
                    direct_mode_handles, adjoint_vec_handles)

                # Compute projection coefficients using BPOD object, which
                # avoids actually manipulating handles and computing inner
                # products, instead using elements of the decomposition for a
                # more efficient computation.
                direct_proj_coeffs = BPOD.compute_proj_coeffs()
                adjoint_proj_coeffs = BPOD.compute_adjoint_proj_coeffs()

                # Test values
                np.testing.assert_allclose(
                    direct_proj_coeffs, direct_proj_coeffs_true,
                    rtol=rtol, atol=atol)
                np.testing.assert_allclose(
                    adjoint_proj_coeffs, adjoint_proj_coeffs_true,
                    rtol=rtol, atol=atol)


if __name__ == '__main__':
    unittest.main()

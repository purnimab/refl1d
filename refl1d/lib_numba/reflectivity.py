import numba
from numpy import fabs, sqrt, exp

_REFL_SIG = 'c16(i8, f8, f8[:], f8[:], f8[:], f8[:])'
_REFL_LOCALS = {
    "cutoff": numba.float64,
    "next": numba.int64,
    "sigma_offset": numba.int64,
    "step": numba.int8,
    "pi4": numba.float64,
    "kz_sq": numba.float64,
    "k": numba.complex128,
    "k_next": numba.complex128,
    "F": numba.complex128,
    "J": numba.complex128,
}
_REFL_LOCALS.update(("B{i}{j}".format(i=i, j=j), numba.complex128)
                    for i in range(1, 3) for j in range(1, 3))
_REFL_LOCALS.update(("M{i}{j}".format(i=i, j=j), numba.complex128)
                    for i in range(1, 3) for j in range(1, 3))
_REFL_LOCALS.update(("C{i}".format(i=i), numba.complex128)
                    for i in range(1, 3))


@numba.njit(_REFL_SIG, parallel=False, cache=True, locals=_REFL_LOCALS)
def refl(layers, kz, depth, sigma, rho, irho):

    J = 1j

    # // Check that Q is not too close to zero.
    # // For negative Q, reverse the layers.
    cutoff = 1e-10
    sigma_offset = 0
    if (kz >= cutoff):
        next = 0
        step = 1
    elif (kz <= -cutoff):
        next = layers-1
        step = -1
        sigma_offset = -1
    else:
        return complex(-1, 0)

    # // Since sqrt(1/4 * x) = sqrt(x)/2, I'm going to pull the 1/2 into the
    # // sqrt to save a multiplication later.
    pi4 = 12.566370614359172e-6  # // 1e-6 * 4 pi
    kz_sq = kz*kz + pi4*rho[next]  # // kz^2 + 4 pi Vrho
    k = fabs(kz)

    B11 = B22 = 1
    B12 = B21 = 0

    for i in range(layers-1):
        # // The loop index is not the layer number because we may be reversing
        # // the stack.  Instead, n is set to the incident layer (which may be
        # // first or last) and incremented or decremented each time through.
        k_next = sqrt(kz_sq - pi4*complex(rho[next+step], irho[next+step]))
        F = (k-k_next)/(k+k_next)*exp(-2.*k*k_next * sigma[sigma_offset + next]**2)
        M11 = exp(J*k*depth[next]) if i > 0 else 1.0
        M22 = exp(-J*k*depth[next]) if i > 0 else 1.0
        M21 = F*M11
        M12 = F*M22

        # // Multiply existing layers B by new layer M
        # // We have unrolled the matrix multiply for speed.
        C1 = B11*M11 + B21*M12
        C2 = B11*M21 + B21*M22
        B11 = C1
        B21 = C2
        C1 = B12*M11 + B22*M12
        C2 = B12*M21 + B22*M22
        B12 = C1
        B22 = C2
        next += step
        k = k_next

    # // And we are done.
    return B12/B11


REFLAMP_SIG = 'void(f8[:], f8[:], f8[:,:], f8[:,:], f8[:], i4[:], c16[:])'


@numba.njit(REFLAMP_SIG, parallel=False, cache=True, locals={"offset": numba.int64})
def reflectivity_amplitude(depth, sigma, rho, irho, kz, rho_index, r):
    layers = len(depth)
    points = len(kz)
    for i in range(points):
        offset = rho_index[i]
        r[i] = refl(layers, kz[i], depth, sigma, rho[offset], irho[offset])

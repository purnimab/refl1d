#pylint: disable=invalid-name
# This program is in the public domain
# Author: Paul Kienzle
"""
Experiment definition

An experiment combines the sample definition with a measurement probe
to create a fittable reflectometry model.
"""
from __future__ import division, print_function

import sys
import os
from math import pi, log10, floor
import traceback
import json
from warnings import warn

import numpy as np
from bumps import parameter
from bumps.parameter import Parameter, to_dict

from . import material, profile
from . import __version__
from .reflectivity import reflectivity_amplitude as reflamp
from .reflectivity import magnetic_amplitude as reflmag
from .reflectivity import BASE_GUIDE_ANGLE as DEFAULT_THETA_M
from .probe import PolarizedNeutronProbeSumDiff, meanreflectivity, splitting
#print("Using pure python reflectivity calculator")
#from .abeles import refl as reflamp
from .util import asbytes

def plot_sample(sample, instrument=None, roughness_limit=0):
    """
    Quick plot of a reflectivity sample and the corresponding reflectivity.
    """
    if instrument is None:
        from .probe import NeutronProbe
        probe = NeutronProbe(T=np.arange(0, 5, 0.05), L=5)
    else:
        probe = instrument.simulate()
    experiment = Experiment(sample=sample, probe=probe,
                            roughness_limit=roughness_limit)
    experiment.plot()

class ExperimentBase(object):
    probe = None # type: probe.Probe
    interpolation = 0
    _probe_cache = None
    _substrate = None
    _surface = None
    def parameters(self):
        raise NotImplementedError()

    def to_dict(self):
        raise NotImplementedError()

    def reflectivity(self, resolution=True, interpolation=0):
        raise NotImplementedError()

    def magnetic_step_profile(self):
        raise NotImplementedError()

    def slabs(self):
        raise NotImplementedError()

    def magnetic_slabs(self):
        raise NotImplementedError()

    def step_profile(self):
        raise NotImplementedError()

    def smooth_profile(self, dz=0.1):
        raise NotImplementedError()

    def plot_profile(self, plot_shift=0.):
        raise NotImplementedError()

    def format_parameters(self):
        p = self.parameters()
        print(parameter.format(p))

    def update_composition(self):
        """
        When the model composition has changed, we need to lookup the
        scattering factors for the new model.  This is only needed
        when an existing chemical formula is modified; new and
        deleted formulas will be handled automatically.
        """
        self._probe_cache.reset()
        self.update()

    def is_reset(self):
        """
        Returns True if a model reset was triggered.
        """
        return self._cache == {}

    def update(self):
        """
        Called when any parameter in the model is changed.

        This signals that the entire model needs to be recalculated.
        """
        # if we wanted to be particularly clever we could predefine
        # the optical matrices and only adjust those that have changed
        # as the result of a parameter changing.   More trouble than it
        # is worth, methinks.
        #print("reseting calculation")
        self._cache = {}

    def residuals(self):
        if 'residuals' not in self._cache:
            # Trigger reflectivity calculation even if there is no data to
            # compare against so that we can profile simulation code, and
            # so that simulation smoke tests are run more thoroughly.
            QR = self.reflectivity()
            if ((self.probe.polarized
                 and all(x is None or x.R is None for x in self.probe.xs))
                    or (not self.probe.polarized and self.probe.R is None)):
                resid = np.zeros(0)
            else:
                if self.probe.polarized:
                    resid = np.hstack([(xs.R - QRi[1])/xs.dR
                                       for xs, QRi in zip(self.probe.xs, QR)
                                       if xs is not None])
                else:
                    resid = (self.probe.R - QR[1])/self.probe.dR
            self._cache['residuals'] = resid
            #print(("%12s "*4)%("Q", "R", "dR", "Rtheory"))
            #print("\n".join(("%12.6e "*4)%el for el in zip(QR[0], self.probe.R, self.probe.dR, QR[1]))
            #print("resid", np.sum(resid**2)/2)

        return self._cache['residuals']

    def numpoints(self):
        if self.probe.polarized:
            return sum(len(xs.Q) for xs in self.probe.xs if xs is not None)
        else:
            return len(self.probe.Q) if self.probe.Q is not None else 0

    def nllf(self):
        """
        Return the -log(P(data|model)).

        Using the assumption that data uncertainty is uncorrelated, with
        measurements normally distributed with mean R and variance dR**2,
        this is just sum( resid**2/2 + log(2*pi*dR**2)/2 ).

        The current version drops the constant term, sum(log(2*pi*dR**2)/2).
        """
        #if 'nllf_scale' not in self._cache:
        #    if self.probe.dR is None:
        #        raise ValueError("No data from which to calculate nllf")
        #    self._cache['nllf_scale'] = np.sum(np.log(2*pi*self.probe.dR**2))
        # TODO: add sigma^2 effects back into nllf; only needs to be calculated
        # when dR changes, so maybe it belongs in probe.
        return 0.5*np.sum(self.residuals()**2) # + self._cache['nllf_scale']

    def plot_reflectivity(self, show_resolution=False,
                          view=None, plot_shift=None):

        n = self.interpolation
        QR = self.reflectivity(interpolation=n)
        self.probe.plot(theory=QR,
                        substrate=self._substrate, surface=self._surface,
                        view=view, plot_shift=plot_shift, interpolation=n)

        if show_resolution:
            import matplotlib.pyplot as plt
            QR = self.reflectivity(resolution=False, interpolation=n)
            if self.probe.polarized:
                # Should be four pairs
                for Q, R in QR:
                    plt.plot(Q, R, ':g')
            else:
                Q, R = QR
                plt.plot(Q, R, ':g')

    def plot(self, plot_shift=None, profile_shift=None, view=None):
        import matplotlib.pyplot as plt
        plt.subplot(211)
        self.plot_reflectivity(plot_shift=plot_shift, view=view)
        plt.subplot(212)
        self.plot_profile(plot_shift=profile_shift)

    def resynth_data(self):
        """Resynthesize data with noise from the uncertainty estimates."""
        self.probe.resynth_data()

    def restore_data(self):
        """Restore original data after resynthesis."""
        self.probe.restore_data()

    def write_data(self, filename, **kw):
        """Save simulated data to a file"""
        self.probe.write_data(filename, **kw)

    def simulate_data(self, noise=2.):
        """
        Simulate a random data set for the model.

        This sets R and dR according to the noise level given.

        **Parameters:**

        *noise*: float or array or None | %
            dR/R uncertainty as a percentage.  If noise is set to None, then
            use dR from the data if present, otherwise default to 2%.
        """
        # TODO: can't do perfect data while setting default uncertainty
        theory = self.reflectivity(resolution=True)
        self.probe.simulate_data(theory, noise=noise)

    def _set_name(self, name):
        self._name = name

    def _get_name(self):
        return self._name if self._name else self.probe.name
    name = property(_get_name, _set_name)

    def save(self, basename):
        self.save_profile(basename)
        #self.save_staj(basename)
        self.save_refl(basename)
        self.save_json(basename)

    def save_json(self, basename):
        """ Save the experiment as a json file """
        try:
            experiment = to_dict(self)
            experiment['refl1d'] = __version__
            json_file = basename + "-expt.json"
            with open(json_file, 'w') as fid:
                data = json.dumps(experiment)
                fid.write(data)
        except Exception:
            traceback.print_exc()
            warn("failed to create json structure for model")

    def save_profile(self, basename):
        if self.ismagnetic:
            self._save_magnetic(basename)
        else:
            self._save_nonmagnetic(basename)

    def _save_magnetic(self, basename):
        # Slabs
        A = np.array(self.magnetic_slabs())
        header = ("# %17s %20s %20s %20s %20s %20s\n"
                  %("thickness (A)", "interface (A)",
                    "rho (1e-6/A^2)", "irho (1e-6/A^2)",
                    "rhoM (1e-6/A^2)", "theta (degrees)"))
        with open(basename+"-slabs.dat", "wb") as fid:
            fid.write(asbytes(header))
            np.savetxt(fid, A.T, fmt="%20.15g")

        # Step profile
        A = np.array(self.magnetic_step_profile())
        header = ("# %10s %12s %12s %12s %12s\n"
                  %("z (A)", "rho (1e-6/A2)", "irho (1e-6/A2)",
                    "rhoM (1e-6/A2)", "theta (degrees)"))
        with open(basename+"-steps.dat", "wb") as fid:
            fid.write(asbytes(header))
            np.savetxt(fid, A.T, fmt="%12.8f")

        # Smooth profile
        A = np.array(self.magnetic_smooth_profile())
        header = ("# %10s %12s %12s %12s %12s\n"
                  %("z (A)", "rho (1e-6/A2)", "irho (1e-6/A2)",
                    "rhoM (1e-6/A2)", "theta (degrees)"))
        with open(basename+"-profile.dat", "wb") as fid:
            fid.write(asbytes(header))
            np.savetxt(fid, A.T, fmt="%12.8f")

    def _save_nonmagnetic(self, basename):
        # Slabs
        A = np.array(self.slabs())
        header = ("# %17s %20s %20s %20s\n"
                  %("thickness (A)", "interface (A)", "rho (1e-6/A^2)",
                    "irho (1e-6/A^2)"))
        with open(basename+"-slabs.dat", "wb") as fid:
            fid.write(asbytes(header))
            np.savetxt(fid, A.T, fmt="%20.15g")

        # Step profile
        A = np.array(self.step_profile())
        header = ("# %10s %20s %20s\n"
                  %("z (A)", "rho (1e-6/A2)", "irho (1e-6/A2)"))
        with open(basename+"-steps.dat", "wb") as fid:
            fid.write(asbytes(header))
            np.savetxt(fid, A.T, fmt="%12.8f")

        # Smooth profile
        A = np.array(self.smooth_profile())
        header = ("# %10s %12s %12s\n"
                  %("z (A)", "rho (1e-6/A2)", "irho (1e-6/A2)"))
        with open(basename+"-profile.dat", "wb") as fid:
            fid.write(asbytes(header))
            np.savetxt(fid, A.T, fmt="%12.8f")

    def save_refl(self, basename):
        # Reflectivity
        theory = self.reflectivity()
        self.probe.save(filename=basename+"-refl.dat", theory=theory,
                        substrate=self._substrate, surface=self._surface)
        if self.interpolation > 0:
            theory = self.reflectivity(interpolation=self.interpolation)
            self.probe.save(filename=basename+"-refl-interp.dat",
                            theory=theory)



class Experiment(ExperimentBase):
    """
    Theory calculator.  Associates sample with data, Sample plus data.
    Associate sample with measurement.

    The model calculator is specific to the particular measurement technique
    that was applied to the model.

    Measurement properties:

        *probe* is the measuring probe

    Sample properties:

        *sample* is the model sample
        *step_interfaces* use slabs to approximate gaussian interfaces
        *roughness_limit* limit the roughness based on layer thickness
        *dz* minimum step size for computed profile steps in Angstroms
        *dA* discretization condition for computed profiles

    If *step_interfaces* is True, then approximate the interface using
    microslabs with step size *dz*.  The microslabs extend throughout
    the whole profile, both the interfaces and the bulk; a value
    for *dA* should be specified to save computation time.  If False, then
    use the Nevot-Croce analytic expression for the interface between slabs.

    The *roughness_limit* value should be reasonably large (e.g., 2.5 or above)
    to make sure that the Nevot-Croce reflectivity calculation matches the
    calculation of the displayed profile.  Use a value of 0 if you want no
    limits on the roughness,  but be aware that the displayed profile may
    not reflect the actual scattering densities in the material.

    The *dz* step size sets the size of the slabs for non-uniform profiles.
    Using the relation d = 2 pi / Q_max,  we use a default step size of d/20
    rounded to two digits, with 5 |Ang| as the maximum default.  For
    simultaneous fitting you may want to set *dz* explicitly using to
    round(pi/Q_max/10, 1) so that all models use the same step size.

    The *dA* condition measures the uncertainty in scattering materials
    allowed when combining the steps of a non-uniform profile into slabs.
    Specifically, the area of the box containing the minimum and the
    maximum of the non-uniform profile within the slab will be smaller
    than *dA*.  A *dA* of 10 gives coarse slabs.  If *dA* is not provided
    then each profile step forms its own slab.  The *dA* condition will
    also apply to the slab approximation to the interfaces.

    *interpolation* indicates the number of points to plot in between
    existing points.

    *smoothness* **DEPRECATED** This parameter is not used.
    """
    profile_shift = 0
    def __init__(self, sample=None, probe=None, name=None,
                 roughness_limit=0, dz=None, dA=None,
                 step_interfaces=None, smoothness=None,
                 interpolation=0):
        # Note: smoothness ignored
        self.sample = sample
        self._substrate = self.sample[0].material
        self._surface = self.sample[-1].material
        self.probe = probe
        self.roughness_limit = roughness_limit
        if dz is None:
            dz = nice((2*pi/probe.Q.max())/10)
            dz = min(dz, 5.0)
        self.dz = dz
        self.dA = dA
        self.step_interfaces = step_interfaces
        self.interpolation = interpolation
        num_slabs = len(probe.unique_L) if probe.unique_L is not None else 1
        self._slabs = profile.Microslabs(num_slabs, dz=dz)
        self._probe_cache = material.ProbeCache(probe)
        self._cache = {}  # Cache calculated profiles/reflectivities
        self._name = name

    @property
    def ismagnetic(self):
        """True if experiment contains magnetic materials"""
        slabs = self._render_slabs()
        return slabs.ismagnetic

    def parameters(self):
        """Fittable parameters to sample and probe"""
        return {
            'sample': self.sample.parameters(),
            'probe': self.probe.parameters(),
            }

    def to_dict(self):
        return to_dict({
            'type': type(self).__name__,
            'name': self.name,
            'sample': self.sample,
            'probe': self.probe,
            'roughness_limit': self.roughness_limit,
            'dz': self.dz,
            'dA': self.dA,
            'step_interfaces': self.step_interfaces,
            'interpolation': self.interpolation,
        })

    def _render_slabs(self):
        """
        Build a slab description of the model from the individual layers.
        """
        key = 'rendered', self.step_interfaces, self.dA
        if key not in self._cache:
            self._slabs.clear()
            self.sample.render(self._probe_cache, self._slabs)
            self._slabs.finalize(step_interfaces=self.step_interfaces,
                                 dA=self.dA)
                                 #roughness_limit=self.roughness_limit)
            self._cache[key] = True
        return self._slabs

    def _reflamp(self):
        #calc_q = self.probe.calc_Q
        #return calc_q, calc_q
        key = 'calc_r'
        if key not in self._cache:
            slabs = self._render_slabs()
            w = slabs.w
            rho, irho = slabs.rho, slabs.irho
            sigma = slabs.sigma
            #sigma = slabs.sigma
            calc_q = self.probe.calc_Q
            #print("calc Q", self.probe.calc_Q)
            if slabs.ismagnetic:
                rhoM, thetaM = slabs.rhoM, slabs.thetaM
                Aguide = self.probe.Aguide.value
                H = self.probe.H.value
                calc_r = reflmag(-calc_q/2, depth=w, rho=rho[0], irho=irho[0],
                                 rhoM=rhoM, thetaM=thetaM, Aguide=Aguide, H=H,
                                 sigma=sigma)
            else:
                calc_r = reflamp(-calc_q/2, depth=w, rho=rho, irho=irho,
                                 sigma=sigma)
            if False and np.isnan(calc_r).any():
                print("w", w)
                print("rho", rho)
                print("irho", irho)
                if slabs.ismagnetic:
                    print("rhoM", rhoM)
                    print("thetaM", thetaM)
                    print("Aguide", Aguide, "H", H)
                print("sigma", sigma)
                print("kz", self.probe.calc_Q/2)
                print("R", abs(np.asarray(calc_r)**2))
                pars = parameter.unique(self.parameters())
                fitted = parameter.varying(pars)
                print(parameter.summarize(fitted))
                print("===")
            self._cache[key] = calc_q, calc_r
            #if np.isnan(calc_q).any(): print("calc_Q contains NaN")
            #if np.isnan(calc_r).any(): print("calc_r contains NaN")
        return self._cache[key]

    def amplitude(self, resolution=False, interpolation=0):
        """
        Calculate reflectivity amplitude at the probe points.
        """
        key = ('amplitude', resolution, interpolation)
        if key not in self._cache:
            calc_q, calc_r = self._reflamp()
            res = self.probe.apply_beam(calc_q, calc_r, resolution=resolution,
                                        interpolation=interpolation)
            self._cache[key] = res
        return self._cache[key]

    def reflectivity(self, resolution=True, interpolation=0):
        """
        Calculate predicted reflectivity.

        If *resolution* is true include resolution effects.
        """
        key = ('reflectivity', resolution, interpolation)
        if key not in self._cache:
            calc_q, calc_r = self._reflamp()
            calc_R = _amplitude_to_magnitude(calc_r,
                                             ismagnetic=self.ismagnetic,
                                             polarized=self.probe.polarized)
            res = self.probe.apply_beam(calc_q, calc_R, resolution=resolution,
                                        interpolation=interpolation)
            self._cache[key] = res
        return self._cache[key]

    def smooth_profile(self, dz=0.1):
        """
        Return the scattering potential for the sample.

        If *dz* is not given, use *dz* = 0.1 A.
        """
        if self.step_interfaces:
            return self.step_profile()
        key = 'smooth_profile', dz
        if key not in self._cache:
            slabs = self._render_slabs()
            prof = slabs.smooth_profile(dz=dz)
            self._cache[key] = prof
        return self._cache[key]

    def step_profile(self):
        """
        Return the step scattering potential for the sample, ignoring
        interfaces.
        """
        key = 'step_profile'
        if key not in self._cache:
            slabs = self._render_slabs()
            prof = slabs.step_profile()
            self._cache[key] = prof
        return self._cache[key]

    def slabs(self):
        """
        Return the slab thickness, roughness, rho, irho for the
        rendered model.

        .. Note::
             Roughness is for the top of the layer.
        """
        slabs = self._render_slabs()
        return (slabs.w, np.hstack((slabs.sigma, 0)),
                slabs.rho[0], slabs.irho[0])

    def magnetic_smooth_profile(self, dz=0.1):
        """
        Return the nuclear and magnetic scattering potential for the sample.
        """
        key = 'magnetic_smooth_profile', '{:.6f}'.format(dz)
        if key not in self._cache:
            slabs = self._render_slabs()
            prof = slabs.magnetic_smooth_profile(dz=dz)
            self._cache[key] = prof
        return self._cache[key]

    def magnetic_step_profile(self):
        """
        Return the nuclear and magnetic scattering potential for the sample.
        """
        key = 'magnetic_step_profile'
        if key not in self._cache:
            slabs = self._render_slabs()
            prof = slabs.magnetic_step_profile()
            self._cache[key] = prof
        return self._cache[key]

    def magnetic_slabs(self):
        slabs = self._render_slabs()
        return (slabs.w, np.hstack((slabs.sigma, 0)),
                slabs.rho[0], slabs.irho[0], slabs.rhoM, slabs.thetaM)

    def save_staj(self, basename):
        from .stajconvert import save_mlayer
        try:
            if self.probe.R is not None:
                datafile = getattr(self.probe, 'filename', basename+".refl")
            else:
                datafile = None
            save_mlayer(self, basename+".staj", datafile=datafile)
            probe = self.probe
            datafile = os.path.join(os.path.dirname(basename), os.path.basename(datafile))
            header = ("# Q R dR\n")
            with open(datafile, "wb") as fid:
                fid.write(asbytes(header))
                np.savetxt(fid, np.vstack((probe.Qo, probe.R, probe.dR)).T)
            fid.close()
        except Exception:
            print("==== could not save staj file ====")
            traceback.print_exc()

    def plot_profile(self, plot_shift=None):
        import matplotlib.pyplot as plt
        from bumps.plotutil import auto_shift

        plot_shift = plot_shift if plot_shift is not None else Experiment.profile_shift
        trans = auto_shift(plot_shift)
        if self.ismagnetic:
            if not self.step_interfaces:
                z, rho, irho, rhoM, thetaM = self.magnetic_step_profile()
                #rhoM_net = rhoM*np.cos(np.radians(thetaM))
                plt.plot(z, rho, ':g', transform=trans)
                plt.plot(z, irho, ':b', transform=trans)
                plt.plot(z, rhoM, ':r', transform=trans)
                if (abs(thetaM-DEFAULT_THETA_M) > 1e-3).any():
                    ax = plt.twinx()
                    plt.plot(z, thetaM, ':k', axes=ax, transform=trans)
                    plt.ylabel('magnetic angle (degrees)')
            z, rho, irho, rhoM, thetaM = self.magnetic_smooth_profile()
            #rhoM_net = rhoM*np.cos(np.radians(thetaM))
            handles = [
                plt.plot(z, rho, '-g', transform=trans, label='rho')[0],
                plt.plot(z, irho, '-b', transform=trans, label='irho')[0],
                plt.plot(z, rhoM, '-r', transform=trans, label='rhoM')[0],
            ]
            if (abs(thetaM-DEFAULT_THETA_M) > 1e-3).any():
                ax = plt.twinx()
                h = plt.plot(z, thetaM, '-k', axes=ax, transform=trans, label='thetaM')
                handles.append(h[0])
                plt.ylabel('magnetic angle (degrees)')
            plt.xlabel('depth (A)')
            plt.ylabel('SLD (10^6 / A**2)')
            labels = [h.get_label() for h in handles]
            plt.legend(handles=handles, labels=labels)
        else:
            if not self.step_interfaces:
                z, rho, irho = self.step_profile()
                plt.plot(z, rho, ':g', z, irho, ':b', transform=trans)
            z, rho, irho = self.smooth_profile()
            plt.plot(z, rho, '-g', z, irho, '-b', transform=trans)
            plt.legend(['rho', 'irho'])
            plt.xlabel('depth (A)')
            plt.ylabel('SLD (10^6 / A**2)')


    def penalty(self):
        return self.sample.penalty()

class MixedExperiment(ExperimentBase):
    """
    Support composite sample reflectivity measurements.

    Sometimes the sample you are measuring is not uniform.
    For example, you may have one portion of you polymer
    brush sample where the brushes are close packed and able
    to stay upright, whereas a different section of the sample
    has the brushes lying flat.  Constructing two sample
    models, one with brushes upright and one with brushes
    flat, and adding the reflectivity incoherently, you can
    then fit the ratio of upright to flat.

    *samples* the layer stacks making up the models
    *ratio* a list of parameters, such as [3, 1] for a 3:1 ratio
    *probe* the measurement to be fitted or simulated

    *coherent* is True if the length scale of the domains
    is less than the coherence length of the neutron, or false
    otherwise.

    Statistics such as the cost functions for the individual
    profiles can be accessed from the underlying experiments
    using composite.parts[i] for the various samples.
    """
    def __init__(self, samples=None, ratio=None, probe=None,
                 name=None, coherent=False, interpolation=0, **kw):
        self.samples = samples
        self.probe = probe
        self.ratio = [Parameter.default(r, name="ratio %d"%i)
                      for i, r in enumerate(ratio)]
        self.parts = [Experiment(s, probe, **kw) for s in samples]
        self.coherent = coherent
        self.interpolation = interpolation
        self._substrate = self.samples[0][0].material
        self._surface = self.samples[0][-1].material
        self._cache = {}
        self._name = name

    def update(self):
        self._cache = {}
        for p in self.parts: p.update()

    def parameters(self):
        return {
            'samples': [s.parameters() for s in self.samples],
            'ratio': self.ratio,
            'probe': self.probe.parameters(),
            }

    def to_dict(self):
        return to_dict({
            'type': type(self).__name__,
            'name': self.name,
            'samples': self.samples,
            'ratio': self.ratio,
            'probe': self.probe,
            'parts': self.parts,
            'coherent': self.coherent,
            'interpolation': self.interpolation,
        })

    def _reflamp(self):
        """
        Calculate the amplitude of the reflectivity...

        For an incoherent sum, we want to add the squares of the amplitudes,
        with a weighting specified by self.ratio, so the amplitudes
        are scaled by sqrt(self.ratio/total) so when they get squared and added
        the normalization is correct.

        For a coherent sum, just multiply by ratio/total.
        It all comes out in the wash.
        """
        total = sum(r.value for r in self.ratio)
        Qs, Rs = zip(*[p._reflamp() for p in self.parts])
        if not self.coherent:
            Rs = [np.asarray(ri)*np.sqrt(ratio_i.value/total)
                  for ri, ratio_i in zip(Rs, self.ratio)]
        else: # self.coherent == True
            Rs = [np.asarray(ri)*(ratio_i.value/total)
                  for ri, ratio_i in zip(Rs, self.ratio)]
        #print("Rs", Rs)
        return Qs[0], Rs

    def amplitude(self, resolution=False):
        """
        """
        if not self.coherent:
            raise TypeError("Cannot compute amplitude of system which is mixed incoherently")
        key = ('amplitude', resolution)
        if key not in self._cache:
            calc_Q, calc_R = self._reflamp()
            calc_R = np.sum(calc_R, axis=1)
            r_real = self.probe.apply_beam(calc_Q, calc_R.real, resolution=resolution)
            r_imag = self.probe.apply_beam(calc_Q, calc_R.imag, resolution=resolution)
            r = r_real + 1j*r_imag
            self._cache[key] = self.probe.Q, r
        return self._cache[key]


    def reflectivity(self, resolution=True, interpolation=0):
        """
        Calculate predicted reflectivity.

        This will be the weighted sum of the reflectivity from the
        individual systems.  If coherent is set, then the coherent
        sum will be used, otherwise the incoherent sum will be used.

        If *resolution* is true include resolution effects.

        *interpolation* is the number of theory points to show between data
        points.
        """
        key = ('reflectivity', resolution, interpolation)
        if key not in self._cache:
            Q, r = self._reflamp()

            polarized = self.probe.polarized
            ismagnetic = any(p.ismagnetic for p in self.parts)

            # If any reflectivity is magnetic, make all reflectivity magnetic
            if ismagnetic:
                for i, p in enumerate(self.parts):
                    if not p.ismagnetic:
                        r[i] = _polarized_nonmagnetic(r[i])

            # Add the cross sections
            if self.coherent:
                r = np.sum(r, axis=0)
                R = _amplitude_to_magnitude(r, ismagnetic=ismagnetic,
                                            polarized=polarized)
            else:
                R = [_amplitude_to_magnitude(ri, ismagnetic=ismagnetic,
                                             polarized=polarized)
                     for ri in r]
                R = np.sum(R, axis=0)

            # Apply resolution
            res = self.probe.apply_beam(Q, R, resolution=resolution,
                                        interpolation=0)
            self._cache[key] = res
        return self._cache[key]

    def plot_profile(self, plot_shift=None):
        f = np.array([r.value for r in self.ratio], 'd')
        f /= np.sum(f)
        for p in self.parts:
            p.plot_profile(plot_shift=plot_shift)

    def save_profile(self, basename):
        for i, p in enumerate(self.parts):
            p.save_profile("%s-%d"%(basename, i))

    def save_staj(self, basename):
        for i, p in enumerate(self.parts):
            p.save_staj("%s-%d"%(basename, i))

    def penalty(self):
        return sum(s.penalty() for s in self.samples)

def _polarized_nonmagnetic(r):
    """Convert nonmagnetic data to polarized representation.

    Polarized non-magnetic data repeats the reflectivity in the non spin flip
    channels and sets the spin flip channels to zero.
    """
    nsf = r
    sf = 0*r
    return [nsf, sf, sf, nsf]

def _nonpolarized_magnetic(R):
    """Convert magnetic reflectivity to unpolarized representation.

    Unpolarized magnetic data adds the cross-sections of the magnetic
    data incoherently and divides by two.
    """
    return sum(R)/2

def _amplitude_to_magnitude(r, ismagnetic, polarized):
    """
    Compute the reflectivity magnitude
    """
    if ismagnetic:
        R = [abs(xs)**2 for xs in r]
        if not polarized:
            R = _nonpolarized_magnetic(R)
    else:
        R = abs(r)**2
        if polarized:
            R = _polarized_nonmagnetic(R)
    return R


def nice(v, digits=2):
    """Fix v to a value with a given number of digits of precision"""
    if v == 0.:
        return v
    sign = v/abs(v)
    place = floor(log10(abs(v)))
    scale = 10**(place-(digits-1))
    return sign*floor(abs(v)/scale+0.5)*scale

#EIV marginalized_residuals from Paul
def marginalized_residuals(Q, FQ, R, dR, angle_uncertainty=0.002, wavelength=None):
    r"""
    Returns the residuals from an error-in-variables model marginalized over
    the variables.

    *angular_uncertainty* from motor jitter in degrees.

    For error in variables fits with normal uncertainty, start with the
    following model:

    .. math::

        x &=& x_o + \epsilon_1 \text{for} \epsilon_1 ~ N(0, \Delta x^2) \\
        y &=& f(x) + \epsilon_2 \text{for} \epsilon_2 ~ N(0, \Delta y^2) \\

    Use a linear approximation at the nominal measurement location $x_o$ then

    .. math::

        f(x) \approx f'(x_?)(x - x_o) + f(x_o)

    and so

    .. math::

        y &\approx& f'(x_o)(x_o + \epsilon_1 - x_o) + f(x_o) + \epsilon_2 \\
          &=& f(x_o) + f'(x_o)\epsilon_1 + \epsilon_2

    Therefore, measured $y_o$ is distributed as

    .. math::

        y_o &~& f(x_o) + f'(x_o)N(0, \Delta x^2) + N(0, \Delta y^2) \\
            &~& N(f(x_o), (f'(x_o)\Delta x)^2 + \Delta y^2)

    That is, assuming that f(x) is approximately linear over $\Delta x$, then
    simply add $f'(x_o)\Delta x^2$ to the variance in the data.

    We should be sampling $x$ densely enough that we can approximate
    $f'(x)$ using the center point formula,

    .. math::

        f'(x) \approx \frac{f(x_{k+1}) - f(x_{k-1})}{x_{k+1} - x_{k-1}}

    For reflectometry specifically the motor uncertainty $\delta\theta$ is
    in angle and the theory is in $Q$, so we use the following

    .. math::

        Q &=& \frac{4 \pi}{\lambda} \sin(\theta + \delta\theta) \\
          &=& \frac{4 \pi}{\lambda} (\cos \delta\theta \sin \theta
              + \sin \delta\theta \cos \theta)

    Using the small angle approximation and $\cos \theta > 0.96$
    for $\theta < 15^o$, then

    .. math::

        Q &\approx& \frac{4 \pi}{\lambda} (\sin \theta + \delta\theta\cos\theta)
          &\approx& \frac{4 \pi}{\lambda} (\sin \theta + \delta\theta)
          &=$ Q + \frac{4 \pi}{\lambda}\delta\theta

    and so

    .. math::

        $\epsilon_1 = \delta Q = \frac{4 \pi}{\lambda}\delta\theta

    This means that we can compute the residual using

    .. math::

        \frac{dR}{dQ} &\approx& \frac{R_{k+1} - R_{k-1}}{Q_{k+1} - Q_{k-1}} \\
        \Delta R' &=& \sqrt{\Delta R^2
            + \left(\frac{4\pi\delta\theta}{\lambda} \frac{dR}{dQ}\right)^2 }

    You can arrive at the same expression using marginalization over the
    possible incident angles $\theta + \delta\theta$ for each $Q$ point

    .. math::

        P(R | Q) = \int  P(R | Q, \theta) P(\theta)\,\mathrm{d}\theta

    where $P(R | Q, \theta)$ is the usual Gaussian residual for the measurement
    and $P(\theta)$ is a Gaussian uncertainty in angle.
    """

    # slope from center point formula
    if angle_uncertainty == 0.0:
        return (R - FQ)/dR
    # Using small angle approximation to Q = 4 pi/L sin(T + d)
    #    Q = 4 pi / L (cos d sin T + sin d cos T)
    #      ~ 4 pi / L (sin T + d cos T)    since d is small
    #      ~ 4 pi / L (sin T + d)          since cos T > 0.96 for T < 15 degrees
    #      = Q + 4 pi / L d
    #      ~ Q + 2.5 d                     since L in [4, 6] angstroms
    DQ = 4*np.pi/wavelength*np.radians(angle_uncertainty)

    # Quick approx to [ log integral P(R,dR;Q') P(Q') dQ'] for motor position
    # uncertainty P(Q') and gaussian measurement uncertainty P(R;Q') is to
    # increase the size of dR based on the slope at Q and the motor uncertainty.
    dRdQ = (R[2:] - R[:-2])/(Q[2:] - Q[:-2])
    # Leave dR untouched for the initial and final point
    dRp = dR.copy()
    dRp[1:-1] = np.sqrt((dR[1:-1])**2 + (DQ[1:-1]*dRdQ)**2)  # add in quadrature
    return (R - FQ)/(dRp)

# TODO: check curvature in marginalized_residuals()
#
# If $|f(x_k) - \hat f(x_k)| \gtrsim |\delta x f'(x_k)|$ where $\hat f(x)$ is
# the line connecting $f(x_k-\delta x)$ to $f(x_k+\delta x)$ then the curvature
# at $x_k$ is too large for the correction factor. Depending on whether it is
# curving toward or away from the measured data point the scaled residuals
# will be too large or too small.
#
# Better:
# Check $(f(x_k) - \hat f(x_k))^2 > C (\delta x f'(x_k))^2 + \delta y^2$.
# That way we ignore the curvature if it is less than some fraction $C$ of
# the combined uncertainty in x and y together.
#
# What should we do when the curvature check fails?  Maybe warn the user,
# or maybe further tweak the mean and variance used to compute the residuals.
#
# Perhaps an additional term in the Taylor series around $f(x_k)$ will
# do the trick, adding $f''(x_0) \epsilon_1^2 / 2$ to the approximation of $y$.
# That is, $Z = f''/2 X + f' X + f + Y$. Completing the square, this is
# $Z = f''/2 (X + f'/f'')^2 + f - (f'/f'')^2 + Y$. The X expression
# corresponds to a non-central $\chi'^2_1$ distribution.[1] Using the
# gaussian approximation (i.e., the mean and variance) for this distribution[2],
# we can then find $Z = X' + Y$, and combine them into a single gaussian
# approximation as before. Or use the generalized $\chi^2$ distribution,
# though that may be more difficult to work with numerically.[3]
#
# [1] https://stats.stackexchange.com/questions/127612/polynomial-transform-of-a-gaussian-random-variable#comment244825_127612
# [2] https://en.wikipedia.org/wiki/Noncentral_chi-squared_distribution
# [3] https://en.wikipedia.org/wiki/Generalized_chi-squared_distribution


class SumDiffExperiment(Experiment):

    def residuals(self):
        if 'residuals' in self._cache:
            return self._cache['residuals']

        if self.probe.polarized:
            have_data = not all(x is None or x.R is None for x in self.probe.xs)
        else:
            have_data = not (self.probe.R is None)
        if not have_data:
            resid = np.zeros(0)
            self._cache['residuals'] = resid
            return resid

        QR = self.reflectivity()
        # print('in residuals')
        # print(len(QR))
        # for q in QR:
        #     if q is not None:
        #         print(len(q[0]))
        #     else:
        #         print('None')
        # print(len(QR[0][0]))
        # print(len(QR[3][0]))
        # QRmean = meanreflectivity(self.probe.pp.Q,QR[0][0],None,self.probe.pp.Q,QR[3][0],None)
        # print('from data')
        # print(len(measRmean[1]))
        mm, mp, pm, pp = QR
        QRmean = meanreflectivity(pp[0],pp[1],None,mm[0],mm[1],None)
        # print(len(QRmean[1]))
        QRdf = splitting(pp[0],pp[1],None,mm[0],mm[1],None)
        # print(len(QRsa[1]))
        # print(len(self.probe.sa.Q))
#        print(self.probe.df.Q)
        resid = np.hstack([(xs[1] - QRi[1])/xs[2]
            for xs, QRi in zip([(self.probe.sm.Q,self.probe.sm.R,self.probe.sm.dR),(self.probe.df.Q,self.probe.df.R,self.probe.df.dR)], [QRmean,QRdf])
            if xs is not None])
        # print(resid)
        self._cache['residuals'] = resid
        return resid

    #probes contain dummy variables for calculating the correct Q - do not want to include
    def numpoints(self):
        if isinstance(self.probe, PolarizedNeutronProbeSumDiff):
            return sum(len(xs.Q) for xs in (self.probe.sm, self.probe.df) if xs is not None)
        elif self.probe.polarized:
            return sum(len(xs.Q) for xs in self.probe.xs if xs is not None)
        else:
            return len(self.probe.Q) if self.probe.Q is not None else 0


class SumDiffEIVExperiment(Experiment):

    def residuals(self):
        if 'residuals' in self._cache:
            return self._cache['residuals']

        if self.probe.polarized:
            have_data = not all([x is None or x.R is None for x in self.probe.xs])
#            print(have_data)
        else:
            have_data = not (self.probe.R is None)
        if not have_data:
            resid = np.zeros(0)
            self._cache['residuals'] = resid
            return resid

        QR = self.reflectivity()

        mm, mp, pm, pp = QR

        QRmean = meanreflectivity(pp[0],pp[1],None,mm[0],mm[1],None)
        QRdf = splitting(pp[0],pp[1],None,mm[0],mm[1],None)


        resid = np.hstack([marginalized_residuals(QRi[0], QRi[1], xs.R, xs.dR, angle_uncertainty=getattr(xs, 'angle_uncertainty', 0.0), wavelength=xs.L)
        for xs, QRi in zip([self.probe.sm,self.probe.df], [QRmean,QRdf]) if xs is not None])
        # print(resid)
        self._cache['residuals'] = resid
        print("resid", np.sum(resid**2)/2)
        return resid

    #probes contain dummy variables for calculating the correct Q - do not want to include
    def numpoints(self):
        if isinstance(self.probe, PolarizedNeutronProbeSumDiff):
            return sum(len(xs.Q) for xs in (self.probe.sm, self.probe.df) if xs is not None)
        elif self.probe.polarized:
            return sum(len(xs.Q) for xs in self.probe.xs if xs is not None)
        else:
            return len(self.probe.Q) if self.probe.Q is not None else 0
